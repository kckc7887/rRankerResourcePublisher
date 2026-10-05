# Unity bundle decoding uses the installed UnityPy package (MIT).
# Optional limmy114/rizline-tool input is read only as a data literal; its
# JavaScript is neither executed nor ported. See THIRD_PARTY_NOTICES.md for
# dependency licenses and the distinction between code and data references.
from __future__ import annotations

import base64
import http.client
import io
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.parse
import uuid
from pathlib import Path

from .audio import acb_duration
from .core import apply_overrides, atomic_write, check_workers, json_bytes, load_overrides, log_progress, max_combo, parallel_map, sha256, supplement_template, validate_catalog

CONFIG_URL = "https://rizserver.pigeongames.net/game/server_api/v1/dis"
CONFIG_HEADERS = {"game_id": "pigeongames.rizline", "channel_id": "11", "i18n": "zh-CN"}
STATS_URL = "https://raw.githubusercontent.com/limmy114/rizline-tool/a7e1ae23aaae215c36710899af363bc71ae32634/index.html"


class HttpError(RuntimeError):
    def __init__(self, url, status):
        super().__init__(f"HTTP {status}: {url}")
        self.status = status


class Http:
    """One transport/cache boundary for configuration, bundles and optional statistics."""
    def __init__(self, cache, transport="auto"):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.transport = "powershell" if transport == "auto" and os.name == "nt" else "urllib" if transport == "auto" else transport
        self.pwsh = shutil.which("pwsh") if self.transport == "powershell" else None
        self.ssl_context = ssl.create_default_context() if self.transport == "urllib" else None
        if self.transport == "powershell" and not self.pwsh:
            raise ValueError("PowerShell 7 is required for the powershell transport")

    def get(self, url, headers=None, refresh=False):
        if not url.startswith("https://"):
            raise ValueError("Upstream resources must use HTTPS")
        key = sha256(json_bytes([url, headers or {}]))
        target = self.cache / (key + ".bin")
        if target.exists() and not refresh:
            return target.read_bytes()
        for attempt in range(3):
            try:
                if self.transport == "powershell":
                    data = self._powershell(url, headers or {})
                else:
                    try:
                        encoded_url = urllib.parse.quote(url, safe=":/?=&%#+@")
                        with urllib.request.urlopen(urllib.request.Request(encoded_url, headers=headers or {}), timeout=90, context=self.ssl_context) as response:
                            data = response.read()
                    except urllib.error.HTTPError as error:
                        raise HttpError(url, error.code) from error
                atomic_write(target, data)
                return data
            except (HttpError, OSError, http.client.HTTPException, subprocess.SubprocessError) as error:
                if isinstance(error, HttpError) and error.status not in (408, 429, 500, 502, 503, 504):
                    raise
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def _powershell(self, url, headers):
        target = self.cache / ("download-" + uuid.uuid4().hex + ".tmp")
        # Script is fixed; untrusted URL/path/header strings only enter through stdin JSON.
        script = """$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$request=[Console]::In.ReadToEnd() | ConvertFrom-Json -AsHashtable
try { Invoke-WebRequest -Uri $request.url -Headers $request.headers -OutFile $request.path -TimeoutSec 90; @{status=200} | ConvertTo-Json -Compress }
catch { $status=0; if ($_.Exception.Response) { $status=[int]$_.Exception.Response.StatusCode }; @{status=$status} | ConvertTo-Json -Compress }
"""
        try:
            result = subprocess.run([self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], input=json_bytes({"url": url, "headers": headers, "path": str(target.resolve())}), capture_output=True, timeout=110, check=True)
            status = json.loads(result.stdout.decode("utf-8-sig"))["status"]
            if status != 200:
                if not status:
                    raise OSError("Network request failed: " + url)
                raise HttpError(url, status)
            return target.read_bytes()
        finally:
            target.unlink(missing_ok=True)


class Addressables:
    """Decode public Unity Addressables bucket/key/entry tables, including multi-dependencies."""
    def __init__(self, value):
        self.value = value
        keys, buckets, entries = (base64.b64decode(value[name]) for name in ("m_KeyDataString", "m_BucketDataString", "m_EntryDataString"))
        self.entries = entries
        self.rows = []
        position = 4
        for _ in range(self.integer(buckets, 0)):
            offset, count = self.integer(buckets, position), self.integer(buckets, position + 4)
            position += 8
            kind = keys[offset]
            key = None
            if kind in (0, 1):
                size = self.integer(keys, offset + 1)
                key = keys[offset + 5:offset + 5 + size].decode("utf-8" if kind == 0 else "utf-16le")
            elif kind == 4:
                key = self.integer(keys, offset + 1)
            indexes = [self.integer(buckets, position + 4 * i) for i in range(count)]
            position += 4 * count
            self.rows.append((key, indexes))
        self.by_key = {key: indexes for key, indexes in self.rows if isinstance(key, str)}

    @staticmethod
    def integer(data, offset):
        return struct.unpack_from("<i", data, offset)[0]

    def bundles(self, key):
        seen, result = set(), []

        def visit(index):
            if index in seen:
                return
            seen.add(index)
            start = 4 + 28 * index
            identity, _, dependency = [self.integer(self.entries, start + 4 * i) for i in range(3)]
            internal = self.value["m_InternalIds"][identity]
            if internal.endswith(".bundle"):
                result.append(internal)
            if dependency >= 0:
                for child in self.rows[dependency][1]:
                    visit(child)

        for index in self.by_key.get(key, []):
            visit(index)
        return list(dict.fromkeys(result))


def normalize_title(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def parse_stats(data):
    source = data.decode("utf-8-sig")
    match = re.search(r"(?:let|const|var)\s+songAllData\s*=\s*(\[.*?\])\s*;", source, re.S)
    if not match:
        raise ValueError("Pinned statistics table is not present")
    # The selected source contains a JSON literal with comment lines and trailing commas.
    literal = re.sub(r"^\s*//[^\n]*", "", match.group(1), flags=re.M)
    literal = re.sub(r",\s*([\]}])", r"\1", literal)
    rows = json.loads(literal)
    if not isinstance(rows, list) or any(not isinstance(row.get("name"), str) for row in rows):
        raise ValueError("Invalid statistics table")
    return rows


def chart_stats(value):
    notes = [note for line in value["lines"] for note in line["notes"]]
    hit = len(notes) + sum(note["type"] == 2 for note in notes)
    tempos = [round(value["bPM"] * shift["value"], 3) for shift in value.get("bpmShifts", [])] or [value["bPM"]]
    return {"hit": hit, "combo": max_combo(hit)}, (min(tempos), max(tempos))


def verified_stats(row, chart):
    if not row or chart["difficulty"] == "SP":
        return None
    value = row.get(chart["difficulty"])
    if not value or value.get("mHit") != chart["hit"]:
        return None
    count = value.get("mH")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= chart["hit"]:
        return None
    return {"riztimeHit": count, "maxScore": 1_000_000 + 100 * count}


def localizations(data):
    result = {}
    for line in data.decode("utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith(("#", ";")):
            key, value = line.split("=", 1)
            result[key.strip()] = re.sub(r"<[^>]*>", "", value.strip()).replace("\\n", "\n")
    return result


def attach_achievements(songs, translations, overrides):
    by_id = {s["id"]: s for s in songs}
    aliases = {}
    for song in songs:
        if any(c["difficulty"] != "SP" for c in song["charts"]):
            aliases.setdefault(normalize_title(song["title"]), []).append(song["id"])
    unresolved = []
    for key, title in translations.items():
        if not key.startswith("ach.") or not key.endswith(".name"):
            continue
        identity = key[4:-5]
        condition = translations.get(f"ach.{identity}.desc")
        if not condition:
            continue
        explicit = overrides["achievementSongs"].get(identity)
        quoted = [match.group(1) for match in re.finditer(r'[“「"]([^”」"]+)[”」"]', condition) if not condition[match.end():].lstrip().startswith("评价")]
        targets = set(explicit or []) if explicit is not None else {song_id for name in quoted for song_id in aliases.get(normalize_title(name), [])}
        if explicit is not None and (not isinstance(explicit, list) or any(t not in by_id for t in explicit)):
            raise ValueError("Invalid achievementSongs override: " + identity)
        if not targets and quoted:
            unresolved.append({"id": identity, "title": title, "condition": condition, "quoted": quoted})
        for song_id in sorted(targets):
            by_id[song_id]["achievements"].append({"id": identity, "title": title, "condition": condition})
    unknown = set(overrides["achievementSongs"]) - {k[4:-5] for k in translations if k.startswith("ach.") and k.endswith(".name")}
    if unknown:
        raise ValueError("Unknown achievement override IDs: " + ", ".join(sorted(unknown)))
    return unresolved


class Importer:
    def __init__(self, http, log=print):
        self.http, self.log = http, log
        self.addressables = None
        self.base, self.version = "", ""
        self.mapping = {}
        self.baseline = ""

    def configure(self):
        config = json.loads(self.http.get(CONFIG_URL, CONFIG_HEADERS, refresh=True))
        self.config = config["configs"][-1]
        self.base = self.config["resourceBaseUrl"]
        self.version = self.config["resourceVersion"]
        self.addressables = Addressables(json.loads(self.http.get(self.config["resourceUrl"] + "/Android/catalog_catalog.json")))
        version, seen = self.version, set()
        while True:
            if version in seen:
                raise ValueError("Cyclic official patch metadata")
            seen.add(version)
            try:
                metadata = self.http.get(f"{self.base}/{version}/patch_metadata").decode("utf-8-sig").splitlines()
            except HttpError as error:
                if error.status != 404:
                    raise
                self.baseline = version
                break
            self.log("Patch metadata: " + version)
            previous, paths = metadata[0], metadata[1:]
            if not re.fullmatch(r"v[a-zA-Z0-9_]+", previous) or not paths:
                raise ValueError("Unknown official patch metadata shape")
            for path in paths:
                self.mapping.setdefault(path, version)
            version = previous

    def bundle(self, internal):
        marker = "/default/"
        if marker not in internal:
            raise ValueError("Unexpected official bundle path: " + internal)
        path = internal.split(marker, 1)[1]
        version = self.mapping.get(path, self.baseline)
        import UnityPy
        return UnityPy.load(self.http.get(f"{self.base}/{version}/{path}"))

    def default(self):
        for internal in self.addressables.bundles("AssetList"):
            env = self.bundle(internal)
            for obj in env.objects:
                if obj.type.name == "MonoBehaviour":
                    data = obj.read_typetree()
                    if "levels" in data and "charts" in data and "musics" in data:
                        return data
        raise ValueError("Official AssetList Default was not found")

    def text(self, key):
        for internal in self.addressables.bundles(key):
            env = self.bundle(internal)
            texts = [bytes(obj.read().m_Script) for obj in env.objects if obj.type.name == "TextAsset"]
            if len(texts) == 1:
                return texts[0]
        raise ValueError("Unambiguous TextAsset not found: " + key)

    def cover(self, key):
        for internal in self.addressables.bundles(key):
            env = self.bundle(internal)
            textures = [obj for obj in env.objects if obj.type.name == "Texture2D"]
            if len(textures) == 1:
                image = textures[0].read().image
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                return buffer.getvalue()
        raise ValueError("Unambiguous cover texture not found: " + key)

    def acb(self, music_id):
        key = f"Assets/GameAssets/CRIAsset/{music_id}.acb"
        references = [value for value in self.addressables.bundles(key) if "/cridata_assets_criaddressables/" in value and ".acb=" in value]
        if len(references) != 1:
            raise ValueError("Unambiguous music ACB not found: " + music_id)
        path = references[0].split("/default/", 1)[1].removesuffix(".bundle")
        version = self.mapping.get(path, self.baseline)
        return self.http.get(f"{self.base}/{version}/{path}")

    def duration(self, music_id):
        return acb_duration(self.acb(music_id))


def import_catalog(work, cache, overrides_path, transport="auto", workers=4, stats_url=STATS_URL, log=None):
    check_workers(workers, 8)
    log = log or log_progress
    work = Path(work)
    overrides = load_overrides(overrides_path)
    importer = Importer(Http(cache, transport), log)
    importer.configure()
    official = importer.default()
    atomic_write(work / "official-default.json", json_bytes(official))
    log(f"Official game {importer.config['version']}: {len(official['levels'])} normal / {len(official['discOLevels'])} special levels")
    # Keep raw discovery separate from the application contract for audit and future parser updates.
    musics = {m["id"]: m for m in official["musics"]}
    illustrations = {m["id"]: m for m in official["illustrations"]}
    charts = {c["id"]: c for c in official["charts"]}
    labels = localizations(importer.text("local.zh-Hans"))
    replacements = {pair["oldKey"]: pair["newKey"] for item in official["resourceReplacements"] if item["withFeature"] == "pigeonCN" for pair in item["pairs"]}
    songs, cover_keys, music_ids, jobs = [], {}, {}, {}
    skipped_previews = []
    for level in official["levels"] + official["discOLevels"]:
        level_charts = [charts[identity] for identity in level["chartIds"]]
        if (level_charts
                and all(chart["level"] in ("EZ", "HD", "IN") and chart["difficulty"] == 99 for chart in level_charts)
                and all(not importer.addressables.bundles(replacements.get(chart["id"], chart["id"])) for chart in level_charts)):
            skipped_previews.append({"songId": level["id"], "chartIds": list(level["chartIds"]), "reason": "preview-charts-not-indexed"})
            log(f"Skipped preview song {level['id']}: all charts have difficulty 99 and are not indexed", flush=True)
            continue
        music, illustration = musics[level["musicId"]], illustrations[level["illustrationId"]]
        pack = level.get("discName") or "disc-o"
        song = {"id": level["id"], "title": music["musicName"], "artist": music.get("artist") or None, "illustrator": illustration.get("artist") or None, "packId": pack, "packName": pack, "bpm": None, "durationSeconds": None, "updatedAt": None, "coverPath": None, "audioPath": None, "charts": [], "achievements": []}
        cover_keys[song["id"]] = replacements.get(level["illustrationId"], level["illustrationId"])
        music_ids[song["id"]] = level["musicId"]
        for identity in level["chartIds"]:
            raw = charts[identity]
            difficulty = raw["level"]
            constant = None if difficulty == "SP" else round(raw["difficulty"], 1)
            label = str(level.get("difficultyText") or "SP") if difficulty == "SP" else str(int(constant)) + ("+" if round(constant * 10) % 10 >= 6 else "")
            designer = raw.get("designer") or None
            chart = {"id": identity, "songId": song["id"], "difficulty": difficulty, "level": label, "constant": constant, "designer": labels.get(designer, designer), "hit": None, "combo": None, "maxScore": None, "riztimeHit": None, "chartPath": None}
            song["charts"].append(chart)
            jobs[identity] = chart
        songs.append(song)
    tasks = [("chart", key) for key in jobs] + [("cover", key) for key in sorted(set(cover_keys.values()))] + [("audio", key) for key in sorted(set(music_ids.values()))]
    results, failures = {}, []

    def retrieve(task):
        kind, key = task
        try:
            if kind == "chart":
                raw = importer.text(replacements.get(key, key))
                return task, (chart_stats(json.loads(raw)), raw), None
            if kind == "cover":
                return task, importer.cover(key), None
            data = importer.acb(key)
            return task, (acb_duration(data), data), None
        except Exception as error:
            failure = {"kind": kind, "id": key, "error": str(error)}
            log(f"{failure['kind']} failed {failure['id']}: {failure['error']}", flush=True)
            return task, None, failure

    def report(done, total, task):
        kind, key = task
        log(f"{kind} {done}/{total} {key}", flush=True)
    for task, result, failure in parallel_map(retrieve, tasks, workers, progress=report):
        if failure:
            failures.append(failure)
        else:
            results[task] = result
    log(f"Imported {len(results)}/{len(tasks)} chart/cover/audio assets; failures={len(failures)}", flush=True)
    atomic_write(work / "import-failures.json", json_bytes(failures))
    if failures:
        raise ValueError(f"{len(failures)} official resources failed; see {work / 'import-failures.json'} (catalog unchanged)")
    statistics = parse_stats(importer.http.get(stats_url)) if stats_url else []
    by_title = {}
    for row in statistics:
        by_title.setdefault(normalize_title(row["name"]), []).append(row)
    unresolved_stats = []
    for song in songs:
        bpms = set()
        match_name = overrides["statAliases"].get(song["id"], song["title"])
        matches = by_title.get(normalize_title(match_name), [])
        row = matches[0] if len(matches) == 1 else None
        for chart in song["charts"]:
            (values, bpm), raw = results[("chart", chart["id"])]
            chart.update(values)
            bpms.update(bpm)
            verified = verified_stats(row, chart)
            if verified:
                chart.update(verified)
            elif chart["difficulty"] != "SP":
                unresolved_stats.append({"songId": song["id"], "title": song["title"], "chartId": chart["id"], "difficulty": chart["difficulty"], "hit": chart["hit"], "matchedStatistics": row["name"] if row else None, "reason": "no-title-match" if not matches else "ambiguous-title-match" if len(matches) > 1 else "missing-or-incompatible-chart-statistics", "statisticsCandidates": [{"name": candidate["name"], "chart": candidate.get(chart["difficulty"])} for candidate in matches]})
            path = f"charts/{sha256(raw)}.json"
            atomic_write(work / path, raw)
            chart["chartPath"] = path
        minimum, maximum = min(bpms), max(bpms)
        song["bpm"] = f"{minimum:g}" if minimum == maximum else f"{minimum:g}–{maximum:g}"
        duration, audio = results[("audio", music_ids[song["id"]])]
        song["durationSeconds"] = duration
        audio_path = f"audio/{sha256(audio)}.acb"
        atomic_write(work / audio_path, audio)
        song["audioPath"] = audio_path
        cover = results[("cover", cover_keys[song["id"]])]
        path = f"covers/{sha256(cover)}.png"
        atomic_write(work / path, cover)
        song["coverPath"] = path
    translations = localizations(importer.text("local.zh-Hans.achievement"))
    unresolved_achievements = attach_achievements(songs, translations, overrides)
    catalog = {"schemaVersion": 1, "resourceVersion": importer.version, "gameVersion": importer.config["version"], "songs": songs}
    summary = validate_catalog(catalog)
    report = {"officialConfigUrl": CONFIG_URL, "officialResourceVersion": importer.version, "gameVersion": importer.config["version"], "statisticsUrl": stats_url, "statisticsPolicy": "Title/explicit-ID alias match AND official chart HIT equality; otherwise null", "bpmPolicy": "Range of official chart bPM multiplied by bpmShifts.value; rounded to 3 decimals", "durationPolicy": "Official ACB WaveformTable NumSamples/SamplingRate, crosschecked against complete HCA frame count minus encoder delay/padding", "unresolvedStatistics": unresolved_stats, "unresolvedAchievements": unresolved_achievements, "summary": summary}
    report["skippedPreviewSongs"] = skipped_previews
    atomic_write(work / "import-report.json", json_bytes(report))
    atomic_write(work / "supplement-template.json", json_bytes(supplement_template(apply_overrides(catalog, overrides))))
    atomic_write(work / "catalog.json", json_bytes(catalog))
    return report
