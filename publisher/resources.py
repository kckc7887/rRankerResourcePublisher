from __future__ import annotations

import copy
import json
import mimetypes
from pathlib import Path

from .storage import Storage, digest, encode, file_digest, object_key, safe_path

KYOU_TABLES = ("songs", "aliases", "charts", "tag_votes", "tag_catalog")
KYOU_FILES = tuple(f"{name}.{extension}" for name in KYOU_TABLES for extension in ("json", "csv")) + ("data.json",)
KYOU_COUNTS = {"songs.json": "songs_rows", "aliases.json": "aliases_rows", "charts.json": "charts_rows", "tag_votes.json": "tag_vote_rows"}


def validate_kyou_rows(name, data, manifest):
    if name in KYOU_COUNTS and len(json.loads(data)) != manifest[KYOU_COUNTS[name]]:
        raise ValueError(f"Kyou row count mismatch: {name}")


def content_type(path):
    return {".m4a": "audio/mp4", ".ogg": "audio/ogg", ".tsv": "text/tab-separated-values",
            ".csv": "text/csv", ".json": "application/json", ".png": "image/png", ".txt": "text/plain"}.get(
        Path(path).suffix, mimetypes.guess_type(path)[0] or "application/octet-stream")


def normalize(game, legacy, assets, output, catalog=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    objects, files, mapping, logical_paths = [], [], {}, set()
    for asset in sorted(assets, key=lambda row: row["logical"]):
        logical = safe_path(asset["logical"])
        if logical in logical_paths:
            raise ValueError("Duplicate logical resource")
        logical_paths.add(logical)
        key = object_key(game, logical, asset["sha256"])
        mapping[asset["original"]] = key
        item = {"key": key, "size": asset["size"], "sha256": asset["sha256"], "contentType": asset.get("contentType") or content_type(logical)}
        item.update({name: asset[name] for name in ("source", "local") if name in asset})
        objects.append(item)
        entry = {name: item[name] for name in ("size", "sha256", "contentType")}
        if game == "phigros":
            entry.update(path=logical, objectKey=key)
        else:
            entry["path"] = key
            if game == "kyou":
                entry["name"] = logical
        files.append(entry)
    if game == "rizline":
        catalog = copy.deepcopy(catalog)
        for song in catalog["songs"]:
            if song["coverPath"]:
                song["coverPath"] = mapping[song["coverPath"]]
            song["audioPath"] = mapping[song["audioPath"]]
            for chart in song["charts"]:
                chart["chartPath"] = mapping[chart["chartPath"]]
        catalog.pop("resourceVersion", None)
        version = digest(encode(catalog))
        catalog["resourceVersion"] = version
        data = encode(catalog)
        sha = digest(data)
        key = object_key(game, "catalog.json", sha)
        local = output / "catalog.json"
        local.write_bytes(data)
        item = {"path": key, "size": len(data), "sha256": sha, "contentType": "application/json"}
        files.append(item)
        objects.append({**item, "key": key, "local": str(local.resolve())})
        manifest = {"schemaVersion": 2, "resourceVersion": version, "gameVersion": legacy["gameVersion"], "catalogPath": key, "files": files}
        pointer = {}
    elif game == "phigros":
        version = digest(encode({"gameVersion": legacy["gameVersion"], "assets": files}))
        manifest = {"schemaVersion": 2, "resourceVersion": version, "gameVersion": legacy["gameVersion"],
                    "assetCount": len(files), "totalBytes": sum(row["size"] for row in files), "assets": files}
        by_logical = {row["path"]: row["objectKey"] for row in files}
        pointer = {"gameVersion": legacy["gameVersion"], "catalog": by_logical["catalog.json"], "noteCounts": by_logical["metadata/note_counts.tsv"]}
    else:
        if (not legacy.get("ok") or legacy.get("strategy") == "batch-failed"
                or legacy.get("fallback_failed", 0) or legacy.get("charts_with_tags") != legacy.get("charts_rows")):
            raise ValueError("Incomplete Kyou crawl")
        if {row["name"] for row in files} != set(KYOU_FILES):
            raise ValueError("Kyou tables missing")
        version = digest(encode(files))
        manifest = {**legacy, "schemaVersion": 2, "resourceVersion": version, "files": files}
        pointer = {}
    return {"manifest": manifest, "pointer": pointer, "objects": objects}


def local_candidate(game, root, output):
    root = Path(root).resolve()
    if game == "kyou":
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        assets = []
        for name in KYOU_FILES:
            path = root / name
            validate_kyou_rows(name, path.read_bytes(), manifest)
            assets.append({"logical": name, "original": name, "size": path.stat().st_size, "sha256": file_digest(path), "local": str(path)})
        return normalize(game, manifest, assets, output)
    pointer = json.loads((root / game / "current.json").read_text(encoding="utf-8"))
    key = pointer.get("manifest", pointer.get("manifestPath"))
    safe_path(key)
    raw = (root / key).read_bytes()
    if digest(raw) != pointer["manifestSha256"]:
        raise ValueError("Local manifest digest mismatch")
    manifest = json.loads(raw)
    prefix = key.rsplit("/", 1)[0] + "/"
    assets = []
    for item in manifest["assets" if game == "phigros" else "files"]:
        original = prefix + safe_path(item["path"]) if game == "phigros" else safe_path(item["path"])
        if not original.startswith(prefix):
            raise ValueError("Local release path escapes manifest directory")
        logical = original[len(prefix):]
        path = root / original
        if path.stat().st_size != item["size"] or file_digest(path) != item["sha256"]:
            raise ValueError(f"Local resource corrupted: {logical}")
        if game == "rizline" and logical == "catalog.json":
            continue
        assets.append({**item, "logical": logical, "original": original, "local": str(path)})
    catalog = json.loads((root / manifest["catalogPath"]).read_text(encoding="utf-8")) if game == "rizline" else None
    return normalize(game, manifest, assets, output, catalog)


def migration_candidate(game, output):
    storage = Storage(game)
    sources, assets = [], []
    if game == "kyou":
        manifest, etag, _ = storage.json("kyou/latest/manifest.json")
        sources.append({"key": "kyou/latest/manifest.json", "etag": etag})
        for name in KYOU_FILES:
            original = f"kyou/latest/{name}"
            data, etag = storage.get(original)
            validate_kyou_rows(name, data, manifest)
            source = {"key": original, "etag": etag}
            assets.append({"logical": name, "original": original, "source": source, "size": len(data), "sha256": digest(data)})
            sources.append(source)
        result = normalize(game, manifest, assets, output)
    else:
        pointer, etag, _ = storage.json(f"{game}/current.json")
        sources.append({"key": f"{game}/current.json", "etag": etag})
        key = safe_path(pointer.get("manifest", pointer.get("manifestPath")))
        if not key.startswith(f"{game}/releases/") or not key.endswith("/manifest.json"):
            raise ValueError("Legacy manifest outside resource ownership")
        manifest, etag, raw = storage.json(key)
        if pointer.get("manifestSha256") and digest(raw) != pointer["manifestSha256"]:
            raise ValueError("Legacy manifest digest mismatch")
        sources.append({"key": key, "etag": etag})
        prefix = key.rsplit("/", 1)[0] + "/"
        inventory = storage.list(prefix)
        expected = {key}
        catalog = None
        for item in manifest["assets" if game == "phigros" else "files"]:
            original = prefix + safe_path(item["path"]) if game == "phigros" else safe_path(item["path"])
            if not original.startswith(prefix) or original in expected:
                raise ValueError("Duplicate or out-of-scope legacy resource")
            expected.add(original)
            row = inventory.get(original)
            if row is None or row["Size"] != item["size"]:
                raise ValueError(f"Legacy baseline missing or wrong size: {original}")
            source = {"key": original, "etag": row["ETag"]}
            sources.append(source)
            logical = original[len(prefix):]
            if game == "rizline" and logical == "catalog.json":
                data, _ = storage.get(original, row["ETag"])
                if digest(data) != item["sha256"]:
                    raise ValueError("Legacy catalog digest mismatch")
                catalog = json.loads(data)
                continue
            assets.append({**item, "logical": logical, "original": original, "source": source})
        if expected != {path for path in inventory if not path.endswith("/")}:
            raise ValueError("Legacy manifest does not describe the complete release")
        result = normalize(game, manifest, assets, output, catalog)
    result["sources"] = sources
    return result
