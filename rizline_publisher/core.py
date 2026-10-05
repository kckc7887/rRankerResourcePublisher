from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sys
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path, PurePosixPath, PureWindowsPath

DIFFICULTIES = ("EZ", "HD", "IN", "AT", "SP")
SONG_FIELDS = {"id", "title", "artist", "illustrator", "packId", "packName", "bpm", "durationSeconds", "updatedAt", "coverPath", "audioPath", "charts", "achievements"}
CHART_FIELDS = {"id", "songId", "difficulty", "level", "constant", "designer", "hit", "combo", "maxScore", "riztimeHit", "chartPath"}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def check_workers(workers, maximum=16):
    if type(workers) is not int or not 1 <= workers <= maximum:
        raise ValueError(f"Workers must be an integer between 1 and {maximum}")


_PROGRESS_LOCK = threading.Lock()


def log_progress(message, **kwargs):
    with _PROGRESS_LOCK:
        print(message, file=sys.stderr, flush=True)


def parallel_map(function, items, workers=4, progress=None):
    """Bound both running work and queued work; preserve deterministic result order."""
    check_workers(workers)
    try:
        total = len(items)
    except TypeError:
        total = None
    iterator, pending, results = iter(enumerate(items)), {}, {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        def fill():
            while len(pending) < workers * 2:
                item = next(iterator, None)
                if item is None:
                    break
                index, value = item
                pending[executor.submit(function, value)] = (index, value)
        try:
            fill()
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    index, value = pending.pop(future)
                    results[index] = future.result()
                    if progress:
                        progress(len(results), total, value)
                fill()
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    return [results[index] for index in range(len(results))]


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or any(c in value for c in ":?#") or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid relative resource path: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(value).is_absolute():
        raise ValueError(f"Invalid relative resource path: {value!r}")
    return value


def _windows_comparison_path(path):
    # Windows resolve() may return the extended-length spelling for only one
    # operand (especially while parallel writers create long directories).
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return PureWindowsPath(value)


def contained_path(root, value):
    relative_path(value)
    root = Path(root).resolve()
    path = (root / value).resolve()
    comparison_root = _windows_comparison_path(root) if os.name == "nt" else root
    comparison_path = _windows_comparison_path(path) if os.name == "nt" else path
    if not comparison_path.is_relative_to(comparison_root):
        raise ValueError("Resource path escapes its directory")
    return path


def numeric(value, label, integer=False, minimum=0):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"Invalid {label}: {value!r}")
    if integer and int(value) != value:
        raise ValueError(f"{label} must be an integer")


def string(value, label, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {label}")


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or set(catalog) != {"schemaVersion", "resourceVersion", "gameVersion", "songs"} or type(catalog.get("schemaVersion")) is not int or catalog["schemaVersion"] != 1:
        raise ValueError("Unsupported catalog schemaVersion")
    string(catalog.get("resourceVersion"), "resourceVersion")
    string(catalog.get("gameVersion"), "gameVersion")
    if not isinstance(catalog.get("songs"), list) or not catalog["songs"]:
        raise ValueError("Catalog must contain songs")
    song_ids, chart_ids = set(), set()
    for song in catalog["songs"]:
        if set(song) != SONG_FIELDS:
            raise ValueError(f"Unexpected/missing song fields: {song.get('id')}: {set(song) ^ SONG_FIELDS}")
        for field in ("id", "title", "packId", "packName"):
            string(song[field], field)
        if song["id"] in song_ids:
            raise ValueError(f"Duplicate song id: {song['id']}")
        song_ids.add(song["id"])
        for field in ("artist", "illustrator", "bpm"):
            string(song[field], field, nullable=True)
        numeric(song["durationSeconds"], "durationSeconds", minimum=0.001)
        if song["updatedAt"] is not None:
            from datetime import date
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", song["updatedAt"]):
                raise ValueError("updatedAt must be a verified game update date YYYY-MM-DD or null")
            date.fromisoformat(song["updatedAt"])
        if song["coverPath"] is not None:
            relative_path(song["coverPath"])
        string(song["audioPath"], "audioPath")
        relative_path(song["audioPath"])
        if not isinstance(song["charts"], list) or not song["charts"]:
            raise ValueError(f"Song has no charts: {song['id']}")
        kinds = set()
        for chart in song["charts"]:
            if set(chart) != CHART_FIELDS:
                raise ValueError(f"Unexpected/missing chart fields: {chart.get('id')}")
            string(chart["id"], "chart id")
            string(chart["chartPath"], "chartPath")
            relative_path(chart["chartPath"])
            if chart["id"] in chart_ids or chart["songId"] != song["id"]:
                raise ValueError(f"Duplicate/mislinked chart: {chart['id']}")
            chart_ids.add(chart["id"])
            if chart["difficulty"] not in DIFFICULTIES or chart["difficulty"] in kinds:
                raise ValueError(f"Invalid/duplicate difficulty: {chart['id']}")
            kinds.add(chart["difficulty"])
            string(chart["level"], "level")
            string(chart["designer"], "designer", nullable=True)
            for field in ("hit", "combo", "maxScore", "riztimeHit"):
                numeric(chart[field], field, integer=True)
            numeric(chart["constant"], "constant")
            if chart["difficulty"] == "SP" and chart["constant"] is not None:
                raise ValueError("SP does not contribute RKS; its constant must be null")
            if chart["hit"] is not None and chart["combo"] is not None and chart["combo"] != max_combo(chart["hit"]):
                raise ValueError(f"HIT/COMBO mismatch: {chart['id']}")
            if chart["riztimeHit"] is not None:
                if chart["hit"] is None or chart["riztimeHit"] > chart["hit"]:
                    raise ValueError(f"Invalid Riztime Hit: {chart['id']}")
                if chart["maxScore"] != 1_000_000 + 100 * chart["riztimeHit"]:
                    raise ValueError(f"Max Score/Riztime Hit mismatch: {chart['id']}")
        if not isinstance(song["achievements"], list):
            raise ValueError("Achievements must be an array")
        achievement_ids = set()
        for achievement in song["achievements"]:
            if set(achievement) != {"id", "title", "condition"}:
                raise ValueError("Invalid achievement fields")
            for field in achievement:
                string(achievement[field], "achievement " + field)
            if achievement["id"] in achievement_ids:
                raise ValueError("Duplicate song achievement")
            achievement_ids.add(achievement["id"])
    return {"songs": len(song_ids), "charts": len(chart_ids), "covers": sum(s["coverPath"] is not None for s in catalog["songs"]), "audios": len({s["audioPath"] for s in catalog["songs"]}), "chartFiles": len({c["chartPath"] for s in catalog["songs"] for c in s["charts"]}), "missingUpdateDates": sum(s["updatedAt"] is None for s in catalog["songs"]), "missingDurations": sum(s["durationSeconds"] is None for s in catalog["songs"]), "missingMaxScores": sum(c["maxScore"] is None for s in catalog["songs"] for c in s["charts"])}


def max_combo(hit):
    if hit <= 5:
        return hit
    if hit <= 8:
        return 2 * hit - 5
    if hit <= 11:
        return 3 * hit - 13
    return 4 * hit - 24


def load_overrides(path):
    result = read_json(path)
    if result.get("schemaVersion") != 1:
        raise ValueError("Unsupported override schema")
    for name in ("songs", "charts", "statAliases", "achievementSongs"):
        if not isinstance(result.get(name), dict):
            raise ValueError(f"overrides.{name} must be an object")
    for identity, alias in result["statAliases"].items():
        string(identity, "statAliases ID")
        string(alias, "statAliases title")
    return result


def apply_overrides(catalog, overrides):
    result = copy.deepcopy(catalog)
    songs = {s["id"]: s for s in result["songs"]}
    charts = {c["id"]: c for s in result["songs"] for c in s["charts"]}
    for kind, items, allowed in (("songs", songs, SONG_FIELDS - {"id", "charts", "coverPath", "audioPath"}), ("charts", charts, CHART_FIELDS - {"id", "songId", "difficulty", "chartPath"})):
        for identity, patch in overrides[kind].items():
            if identity not in items:
                raise ValueError(f"Unknown override {kind} id: {identity}")
            if not isinstance(patch, dict) or not set(patch).issubset(allowed):
                raise ValueError(f"Unsupported override fields: {kind}/{identity}")
            items[identity].update(patch)
    validate_catalog(result)
    return result


def supplement_template(catalog):
    result = {"schemaVersion": 1, "songs": {}, "charts": {}, "statAliases": {}, "achievementSongs": {}}
    for song in catalog["songs"]:
        missing = {field: None for field in ("artist", "illustrator", "bpm", "durationSeconds", "updatedAt") if song[field] is None}
        if missing:
            result["songs"][song["id"]] = missing
        for chart in song["charts"]:
            fields = ("designer", "hit", "combo", "maxScore", "riztimeHit") + (() if chart["difficulty"] == "SP" else ("constant",))
            missing = {field: None for field in fields if chart[field] is None}
            if missing:
                result["charts"][chart["id"]] = missing
    return result


def build(source, overrides, output, workers=4, transcode_cache=None):
    check_workers(workers)
    log_progress(f"build hash-and-write workers={workers}")
    source, output = Path(source), Path(output)
    catalog = apply_overrides(read_json(source), load_overrides(overrides))
    atomic_write(source.parent / "supplement-template.json", json_bytes(supplement_template(catalog)))
    from .audio import acb_duration
    def hashed_files(paths, verify, label):
        def read(path):
            data = contained_path(source.parent, path).read_bytes()
            verify(data)
            return path, sha256(data), data
        def report(done, total, path):
            log_progress(f"{label} {done}/{total} {path}")
        items = parallel_map(read, sorted(paths), workers, progress=report) if paths else []
        return {digest: data for _, digest, data in items}, {path: digest for path, digest, _ in items}
    def verify_png(data):
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Imported covers must be PNG images")
    from .transcode import is_m4a, transcode_acb
    cache = Path(transcode_cache) if transcode_cache is not None else Path(".cache/transcode")
    def hashed_audio(paths):
        def convert(path):
            acb = contained_path(source.parent, path).read_bytes()
            duration = acb_duration(acb)
            data = transcode_acb(acb, cache, duration)
            if not is_m4a(data):
                raise ValueError(f"Transcoded audio is not M4A: {path}")
            return path, sha256(data), data
        def report(done, total, path):
            log_progress(f"transcode-audio {done}/{total} {path}")
        items = parallel_map(convert, sorted(paths), workers, progress=report) if paths else []
        return {digest: data for _, digest, data in items}, {path: digest for path, digest, _ in items}
    cover_bytes, cover_hashes = hashed_files({s["coverPath"] for s in catalog["songs"] if s["coverPath"]}, verify_png, "hash-cover")
    audio_bytes, audio_hashes = hashed_audio({s["audioPath"] for s in catalog["songs"]})
    chart_bytes, chart_hashes = hashed_files({c["chartPath"] for s in catalog["songs"] for c in s["charts"]}, json.loads, "hash-chart")
    for song in catalog["songs"]:
        if song["coverPath"]:
            song["coverPath"] = cover_hashes[song["coverPath"]]
        song["audioPath"] = audio_hashes[song["audioPath"]]
        for chart in song["charts"]:
            chart["chartPath"] = chart_hashes[chart["chartPath"]]
    fingerprint = sha256(json_bytes(catalog))[:16]
    official_version = re.sub(r"[^a-zA-Z0-9_.-]", "_", catalog["resourceVersion"])
    revision = f"{official_version}-{fingerprint}"
    prefix = f"rizline/releases/{revision}"
    catalog["resourceVersion"] = revision
    payloads = {}
    for song in catalog["songs"]:
        if song["coverPath"]:
            digest = song["coverPath"]
            song["coverPath"] = f"{prefix}/covers/{digest}.png"
            payloads[song["coverPath"]] = cover_bytes[digest]
        digest = song["audioPath"]
        song["audioPath"] = f"{prefix}/audio/{digest}.m4a"
        payloads[song["audioPath"]] = audio_bytes[digest]
        for chart in song["charts"]:
            digest = chart["chartPath"]
            chart["chartPath"] = f"{prefix}/charts/{digest}.json"
            payloads[chart["chartPath"]] = chart_bytes[digest]
    catalog_path = f"{prefix}/catalog.json"
    payloads[catalog_path] = json_bytes(catalog)
    manifest = {"schemaVersion": 1, "resourceVersion": revision, "gameVersion": catalog["gameVersion"], "files": [{"path": path, "size": len(data), "sha256": sha256(data)} for path, data in sorted(payloads.items())], "catalogPath": catalog_path}
    manifest_path = f"{prefix}/manifest.json"
    manifest_bytes = json_bytes(manifest)
    def write_payload(item):
        path, data = item
        target = contained_path(output, path)
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Refusing to overwrite an immutable release: {path}")
        atomic_write(target, data)
    payloads_with_manifest = {**payloads, manifest_path: manifest_bytes}
    def report_write(done, total, item):
        log_progress(f"write {done}/{total} {item[0]}")
    parallel_map(write_payload, payloads_with_manifest.items(), workers, progress=report_write)
    current = {"schemaVersion": 1, "resourceVersion": revision, "manifestPath": manifest_path, "manifestSha256": sha256(manifest_bytes)}
    summary = validate_release(output, current, workers)
    atomic_write(output / "rizline/current.json", json_bytes(current))
    return summary


def validate_current(current):
    if not isinstance(current, dict) or set(current) != {"schemaVersion", "resourceVersion", "manifestPath", "manifestSha256"} or type(current["schemaVersion"]) is not int or current["schemaVersion"] != 1:
        raise ValueError("Invalid current.json")
    if not isinstance(current["resourceVersion"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,200}", current["resourceVersion"]):
        raise ValueError("Invalid resource version")
    if not isinstance(current["manifestSha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", current["manifestSha256"]):
        raise ValueError("Invalid manifest digest")
    prefix = f"rizline/releases/{current['resourceVersion']}/"
    if current["manifestPath"] != prefix + "manifest.json":
        raise ValueError("Manifest is outside the selected release")
    return prefix


def validate_manifest(current, raw):
    prefix = validate_current(current)
    if sha256(raw) != current["manifestSha256"]:
        raise ValueError("Manifest digest mismatch")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or set(manifest) != {"schemaVersion", "resourceVersion", "gameVersion", "files", "catalogPath"} or type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != 1 or manifest["resourceVersion"] != current["resourceVersion"]:
        raise ValueError("Manifest version/fields mismatch")
    string(manifest["gameVersion"], "gameVersion")
    paths = set()
    if not isinstance(manifest["files"], list) or not manifest["files"]:
        raise ValueError("Manifest must contain files")
    for asset in manifest["files"]:
        if not isinstance(asset, dict) or set(asset) != {"path", "size", "sha256"} or type(asset["size"]) is not int or asset["size"] <= 0 or not isinstance(asset["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            raise ValueError("Invalid manifest asset fields")
        path = relative_path(asset["path"])
        if path in paths or not path.startswith(prefix) or path == current["manifestPath"]:
            raise ValueError("Duplicate or cross-release manifest path")
        paths.add(path)
    if manifest["catalogPath"] not in paths:
        raise ValueError("Catalog is not in the manifest")
    return manifest


def validate_catalog_release(catalog, manifest):
    summary = validate_catalog(catalog)
    if catalog["resourceVersion"] != manifest["resourceVersion"] or catalog["gameVersion"] != manifest["gameVersion"]:
        raise ValueError("Catalog release mismatch")
    paths = {asset["path"] for asset in manifest["files"]}
    for song in catalog["songs"]:
        if song["coverPath"] and song["coverPath"] not in paths:
            raise ValueError("Cover is not in the manifest")
        if song["audioPath"] not in paths:
            raise ValueError("Audio is not in the manifest")
        for chart in song["charts"]:
            if chart["chartPath"] not in paths:
                raise ValueError("Chart is not in the manifest")
    return summary


def validate_release(root, current=None, workers=4):
    check_workers(workers)
    root = Path(root)
    current = read_json(root / "rizline/current.json") if current is None else current
    validate_current(current)
    manifest = validate_manifest(current, contained_path(root, current["manifestPath"]).read_bytes())
    log_progress(f"verify {len(manifest['files'])} files")
    def verify_asset(asset):
        data = contained_path(root, asset["path"]).read_bytes()
        if len(data) != asset["size"] or sha256(data) != asset["sha256"]:
            raise ValueError(f"Resource integrity mismatch: {asset['path']}")
    def report(done, total, asset):
        log_progress(f"verify {done}/{total} {asset['path']}")
    parallel_map(verify_asset, manifest["files"], workers, progress=report)
    catalog = read_json(contained_path(root, manifest["catalogPath"]))
    summary = validate_catalog_release(catalog, manifest)
    if any(not song["audioPath"].endswith(".m4a") for song in catalog["songs"]):
        raise ValueError("Published audio must be AAC/M4A")
    return {"resourceVersion": current["resourceVersion"], "files": len(manifest["files"]), "bytes": sum(a["size"] for a in manifest["files"]), **summary}
