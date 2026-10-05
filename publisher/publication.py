from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import Storage, digest, encode, file_digest, owned_key, parallel


def now():
    return datetime.now(timezone.utc).isoformat()


def entries(game, manifest):
    if game == "phigros":
        return [{**asset, "key": asset["objectKey"]} for asset in manifest["assets"]]
    return [{**asset, "key": asset["path"]} for asset in manifest["files"]]


def read_release(storage, game, required=True):
    key = f"{game}/latest.json"
    head = storage.head(key)
    if head is None:
        if required:
            raise ValueError("Missing publication baseline; run the explicit migration first")
        return None
    raw, etag = storage.get(key, head["ETag"])
    pointer = json.loads(raw)
    manifest_key = pointer.get("manifest", pointer.get("manifestPath"))
    owned_key(game, manifest_key, manifests=True)
    if not manifest_key.startswith(f"{game}/manifests/") or pointer["schemaVersion"] != 2:
        raise ValueError("Unsupported publication baseline")
    manifest_raw, _ = storage.get(manifest_key)
    if digest(manifest_raw) != pointer["manifestSha256"] or digest(manifest_raw) != Path(manifest_key).stem:
        raise ValueError("Baseline manifest SHA-256 mismatch")
    manifest = json.loads(manifest_raw)
    if manifest["schemaVersion"] != 2 or manifest["resourceVersion"] != pointer["resourceVersion"]:
        raise ValueError("Baseline manifest version mismatch")
    for item in entries(game, manifest):
        owned_key(game, item["key"])
        if Path(item["key"]).stem != item["sha256"] or item["size"] <= 0:
            raise ValueError("Invalid baseline object")
    return {"pointer": pointer, "manifest": manifest, "etag": etag, "manifestKey": manifest_key}


def prepare(game, candidate, output, *, migration=False):
    storage = Storage(game)
    baseline = read_release(storage, game, required=not migration)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    plan = {"id": uuid.uuid4().hex, "game": game, "bucket": storage.bucket, "preparedAt": now(),
            "baselineEtag": baseline["etag"] if baseline else None,
            "previousManifest": baseline["manifestKey"] if baseline else None,
            "manifest": candidate["manifest"], "pointer": candidate["pointer"],
            "sources": candidate.get("sources", []), "tasks": [], "shards": [],
            "unchanged": bool(baseline and baseline["manifest"]["resourceVersion"] == candidate["manifest"]["resourceVersion"])}
    summary = {name: {"count": 0, "bytes": 0} for name in ("reuse", "copy", "upload", "delete")}
    current = {item["key"]: item for item in entries(game, baseline["manifest"])} if baseline else {}
    targets = storage.list(f"{game}/") if not plan["unchanged"] else {}
    unique = {}
    for item in candidate["objects"]:
        owned_key(game, item["key"])
        if Path(item["key"]).stem != item["sha256"] or item["size"] <= 0:
            raise ValueError("Invalid candidate object")
        if item["key"] in unique and (item["size"], item["sha256"]) != (unique[item["key"]]["size"], unique[item["key"]]["sha256"]):
            raise ValueError("Conflicting object definitions")
        unique[item["key"]] = item
    if {row["key"] for row in entries(game, plan["manifest"])} != set(unique):
        raise ValueError("Manifest and candidate objects disagree")
    for key, item in sorted(unique.items()):
        if key in current:
            if (current[key]["size"], current[key]["sha256"]) != (item["size"], item["sha256"]):
                raise ValueError("Immutable object contract changed")
            mode = "reuse"
        else:
            head = targets.get(key)
            if head:
                if head["Size"] != item["size"]:
                    raise ValueError(f"Existing target has wrong size: {key}")
                storage.verify(key, item["size"], item["sha256"], head["ETag"])
                mode = "reuse"
            else:
                mode = "copy" if "source" in item else "upload"
                if migration and mode == "upload" and "local" not in item:
                    raise ValueError("Migration cannot fall back to downloading upstream resources")
                plan["tasks"].append({**item, "mode": mode})
        summary[mode]["count"] += 1
        summary[mode]["bytes"] += item["size"]
    if plan["unchanged"] and plan["tasks"]:
        raise ValueError("Unchanged release contains different objects")
    shard_count = min(8, len(plan["tasks"]))
    shards = [{"id": index, "bytes": 0, "keys": []} for index in range(shard_count)]
    for item in sorted(plan["tasks"], key=lambda row: (-row["size"], row["key"])):
        shard = min(shards, key=lambda row: (row["bytes"], row["id"]))
        item["shard"] = shard["id"]
        shard["bytes"] += item["size"]
        shard["keys"].append(item["key"])
        if item["mode"] == "upload":
            source = Path(item.pop("local"))
            if source.stat().st_size != item["size"] or file_digest(source) != item["sha256"]:
                raise ValueError(f"Local payload changed: {source.name}")
            target = output / f"shard-{shard['id']}" / item["key"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    for shard in shards:
        (output / f"shard-{shard['id']}").mkdir(exist_ok=True)
        (output / f"shard-{shard['id']}" / "shard.json").write_bytes(encode({"id": shard["id"], "plan": plan["id"]}))
    plan["shards"] = shards
    plan["summary"] = summary
    (output / "plan.json").write_bytes(encode(plan))
    return plan


def upload_shard(plan, shard_id, payload, receipt):
    game = plan["game"]
    storage = Storage(game)
    if storage.bucket != plan["bucket"] or shard_id not in [row["id"] for row in plan["shards"]]:
        raise ValueError("Shard does not belong to this publication")
    results = []
    for item in plan["tasks"]:
        if item["shard"] != shard_id:
            continue
        key = owned_key(game, item["key"])
        head = storage.head(key)
        if head:
            etag = storage.verify(key, item["size"], item["sha256"], head["ETag"])
        else:
            if item["mode"] == "copy":
                source = item["source"]
                allowed = f"{game}/latest/" if game == "kyou" else f"{game}/releases/"
                if not source["key"].startswith(allowed):
                    raise ValueError("Copy source outside resource ownership")
                etag = storage.copy(source["key"], source["etag"], key, item["sha256"], item["contentType"])
            else:
                path = Path(payload) / key
                if path.stat().st_size != item["size"] or file_digest(path) != item["sha256"]:
                    raise ValueError(f"Shard payload corrupted: {key}")
                with path.open("rb") as source:
                    etag = storage.put(key, source, item["contentType"], absent=True, sha256=item["sha256"], immutable=True)
            storage.verify(key, item["size"], item["sha256"], etag)
        results.append({"key": key, "etag": etag})
    result = {"plan": plan["id"], "shard": shard_id, "objects": results}
    Path(receipt).parent.mkdir(parents=True, exist_ok=True)
    Path(receipt).write_bytes(encode(result))
    return result


def finalize(plan, receipts):
    game = plan["game"]
    storage = Storage(game)
    if storage.bucket != plan["bucket"]:
        raise ValueError("Publication bucket changed")
    receipt_map = {}
    for receipt in receipts:
        if receipt["plan"] != plan["id"] or receipt["shard"] in receipt_map:
            raise ValueError("Wrong or duplicate shard receipt")
        receipt_map[receipt["shard"]] = receipt
    if set(receipt_map) != {row["id"] for row in plan["shards"]}:
        raise ValueError("Some child workflows have not completed")
    for shard in plan["shards"]:
        if sorted(row["key"] for row in receipt_map[shard["id"]]["objects"]) != sorted(shard["keys"]):
            raise ValueError("Shard did not verify every planned object")
    latest = read_release(storage, game, required=False)
    if latest and latest["manifest"]["resourceVersion"] == plan["manifest"]["resourceVersion"]:
        return {"unchanged": True, "pointer": latest["pointer"]}
    if (latest["etag"] if latest else None) != plan["baselineEtag"]:
        raise ValueError("Publication baseline changed; refusing pointer update")
    def check_source(source):
        head = storage.head(source["key"])
        if not head or head["ETag"] != source["etag"]:
            raise ValueError(f"Migration source changed: {source['key']}")
    parallel(check_source, plan["sources"])
    verified_etags = {item["key"]: item["etag"] for receipt in receipts for item in receipt["objects"]}
    def check(item):
        key = owned_key(game, item["key"])
        head = storage.head(key)
        if not head or head["ContentLength"] != item["size"]:
            raise ValueError(f"Referenced object is missing: {key}")
        if key in verified_etags and head["ETag"] != verified_etags[key]:
            raise ValueError(f"Verified object changed: {key}")
    parallel(check, entries(game, plan["manifest"]))
    manifest = {**plan["manifest"], "publishedAt": plan["preparedAt"], "previousManifest": plan["previousManifest"]}
    if game == "phigros":
        manifest["generatedAt"] = manifest["publishedAt"]
    body = encode(manifest)
    sha = digest(body)
    key = f"{game}/manifests/{sha}.json"
    head = storage.head(key)
    if head is None:
        storage.put(key, body, "application/json", absent=True, sha256=sha, immutable=True)
    storage.verify(key, len(body), sha)
    pointer = {**plan["pointer"], "schemaVersion": 2, "resourceVersion": manifest["resourceVersion"],
               "manifestSha256": sha, "publishedAt": manifest["publishedAt"]}
    pointer["manifest" if game == "phigros" else "manifestPath"] = key
    raw = encode(pointer)
    etag = storage.put(f"{game}/latest.json", raw, "application/json", etag=plan["baselineEtag"], absent=not plan["baselineEtag"])
    actual, _ = storage.get(f"{game}/latest.json", etag)
    if actual != raw:
        raise ValueError("Pointer readback failed")
    return {"unchanged": False, "pointer": pointer}


def garbage_collect(game, *, execute=False):
    storage = Storage(game)
    release = read_release(storage, game)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    retained = set()
    referenced = set()
    key, manifest, retirement, index = release["manifestKey"], release["manifest"], None, 0
    visited = set()
    while key:
        owned_key(game, key, manifests=True)
        if key in visited:
            raise ValueError("Manifest history has a cycle")
        visited.add(key)
        keep = index < 2 or retirement is None or retirement >= cutoff
        if keep:
            retained.add(key)
            referenced.update(owned_key(game, row["key"]) for row in entries(game, manifest))
        retirement = datetime.fromisoformat(manifest["publishedAt"])
        key = manifest.get("previousManifest")
        if key:
            head = storage.head(key)
            if head is None:
                if keep and index == 0:
                    raise ValueError("Previous manifest missing")
                break
            raw, _ = storage.get(key, head["ETag"])
            if digest(raw) != Path(key).stem:
                raise ValueError("History manifest corrupted")
            manifest = json.loads(raw)
        index += 1
    inventory = storage.list(f"{game}/")
    for key, row in inventory.items():
        if key.startswith(f"{game}/manifests/") and row["LastModified"] >= cutoff and key not in retained:
            owned_key(game, key, manifests=True)
            raw, _ = storage.get(key, row["ETag"])
            if digest(raw) != Path(key).stem:
                raise ValueError("Retained manifest corrupted")
            retained.add(key)
            referenced.update(owned_key(game, asset["key"]) for asset in entries(game, json.loads(raw)))
    candidates = []
    for key, row in inventory.items():
        try:
            owned_key(game, key, manifests=True)
        except ValueError:
            continue
        if key not in retained | referenced and row["LastModified"] < cutoff:
            candidates.append({"key": key, "etag": row["ETag"], "size": row["Size"]})
    if execute:
        for row in candidates:
            head = storage.head(f"{game}/latest.json")
            if head["ETag"] != release["etag"]:
                raise ValueError("Publication changed during cleanup")
            storage.delete(row["key"], row["etag"])
    return candidates
