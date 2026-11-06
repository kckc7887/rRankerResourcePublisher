from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import requests
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from .storage import Storage, digest, encode, file_digest, parallel, recoverable_upload_error, safe_path

SONG_LIST_URL = "https://maimai.lxns.net/api/v0/maimai/song/list"
CHART_URL = "https://assets2.lxns.net/maimai/chart/{chart_id}.txt"
DIFFICULTY_IDS = {"BASIC": 0, "ADVANCED": 1, "EXPERT": 2, "MASTER": 3, "Re:MASTER": 4}
AXES = ("键盘", "星星", "技巧", "体力", "爆发")
SCORE_SCRIPT = Path(__file__).with_name("dxtag-score.mjs")


class DxtagIncomplete(RuntimeError):
    def __init__(self, result):
        super().__init__(f"{len(result['failed'])} DXTag charts failed")
        self.result = result


def object_key(chart_id):
    if type(chart_id) is not int or chart_id < 0:
        raise ValueError(f"Invalid chart id: {chart_id}")
    return safe_path(f"DXTag/{chart_id}.json")


def expected_chart_ids(payload):
    songs = payload.get("songs") if isinstance(payload, dict) else None
    if not isinstance(songs, list):
        raise ValueError("LXNS catalog is missing songs")
    chart_ids = set()
    for song in songs:
        song_id = song.get("id") if isinstance(song, dict) else None
        if type(song_id) is not int:
            raise ValueError("LXNS song id is invalid")
        if song_id > 100000:
            continue
        if song_id < 0 or song_id > 10000:
            raise ValueError(f"Unsupported LXNS song id: {song_id}")
        difficulties = song.get("difficulties")
        if not isinstance(difficulties, dict):
            raise ValueError(f"LXNS song is missing charts: {song_id}")
        standard, dx = difficulties.get("standard", []), difficulties.get("dx", [])
        if not isinstance(standard, list) or not isinstance(dx, list):
            raise ValueError(f"LXNS song charts are invalid: {song_id}")
        if standard:
            chart_ids.add(song_id)
        if dx:
            chart_ids.add(song_id + 10000)
    return chart_ids


def missing_chart_ids(storage, expected):
    chart_ids = sorted(expected)
    try:
        objects = storage.list("DXTag/")
    except (ConnectTimeoutError, ReadTimeoutError):
        print("DXTag listing timed out; checking the expected chart objects", file=sys.stderr, flush=True)
        heads = parallel(lambda chart_id: storage.head(object_key(chart_id)), chart_ids)
        return [chart_id for chart_id, head in zip(chart_ids, heads, strict=True) if head is None]
    return [chart_id for chart_id in chart_ids if object_key(chart_id) not in objects]


def charts_document(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("DXTag returned no charts")
    converted = []
    seen = set()
    for row in rows:
        difficulty = DIFFICULTY_IDS.get(row.get("difficulty") if isinstance(row, dict) else None)
        scores = row.get("scores") if isinstance(row, dict) else None
        if difficulty is None or not isinstance(scores, dict) or set(scores) != set(AXES):
            raise ValueError("DXTag chart shape is invalid")
        if difficulty in seen:
            raise ValueError(f"Duplicate DXTag difficulty: {difficulty}")
        seen.add(difficulty)
        values = []
        for axis in AXES:
            value = scores[axis]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("DXTag score is invalid")
            scaled = round(float(value) * 10)
            if abs(float(value) * 10 - scaled) > 1e-4 or not 0 <= scaled <= 100:
                raise ValueError("DXTag score is out of range")
            values.append(scaled / 10)
        converted.append((difficulty, values))
    converted.sort()
    body = ",".join(
        f'{{"difficulty":{difficulty},"scores":[{",".join(f"{value:.1f}" for value in values)}]}}'
        for difficulty, values in converted
    )
    return f"[{body}]\n".encode()


def _fetch(url):
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=(30, 60))
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def score_chart(chart_id, output):
    if not os.environ.get("DXTAG_ROOT"):
        raise ValueError("DXTAG_ROOT is required")
    response = _fetch(CHART_URL.format(chart_id=chart_id))
    path = output / f"{chart_id}.txt"
    path.write_bytes(response.content)
    try:
        completed = subprocess.run(
            ["node", str(SCORE_SCRIPT), str(path)],
            check=False, capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
    finally:
        path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "DXTag failed").strip())
    return charts_document(json.loads(completed.stdout))


def _upload_chart(storage, chart_id, body, *, refresh=False):
    key = object_key(chart_id)
    if refresh:
        head = storage.head(key)
        if (head and head.get("ContentType") == "application/json"
                and head.get("CacheControl") == "no-cache,no-store,must-revalidate"
                and storage.get(key, head["ETag"])[0] == body):
            return False
    for attempt in range(3 if refresh else 1):
        try:
            etag = storage.put(key, body, "application/json", absent=not refresh)
        except ClientError as error:
            if not refresh and error.response["ResponseMetadata"]["HTTPStatusCode"] == 412:
                return False
            raise
        try:
            storage.verify(key, len(body), digest(body), etag)
            break
        except ValueError:
            if not refresh or attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))
    return True


def _publish_library(storage, expected):
    library = dict(parallel(lambda chart_id: (str(chart_id), storage.json(object_key(chart_id))[0]), sorted(expected)))
    body = encode(library)
    key = "DXTag/all.json"
    head = storage.head(key)
    matches = (head is not None and head["ContentLength"] == len(body)
               and head.get("ContentType") == "application/json"
               and head.get("CacheControl") == "no-cache,no-store,must-revalidate"
               and not head.get("Metadata") and storage.get(key, head["ETag"])[0] == body)
    if matches:
        print(f"Reusing {key}: verified {len(library)} charts", file=sys.stderr, flush=True)
    else:
        etag = storage.put(key, body, "application/json", etag=head["ETag"] if head else None, absent=head is None)
        storage.verify(key, len(body), digest(body), etag)
    return {"key": key, "charts": len(library)}


def publish_prepared(plan_path):
    plan_path = Path(plan_path)
    plan = json.loads(plan_path.read_bytes())
    storage = Storage("dxtag")
    targets = plan["expected"] if plan["refresh"] else plan["missing"]
    if plan["bucket"] != storage.bucket or {row["id"] for row in plan["objects"]} != set(targets):
        raise ValueError("Incomplete prepared DXTag publication")
    for item in plan["objects"]:
        path = plan_path.parent / "payload" / object_key(item["id"])
        if item["id"] not in plan["expected"] or path.stat().st_size != item["size"] or file_digest(path) != item["sha256"]:
            raise ValueError("Prepared DXTag payload changed")
    completed = []
    for item in plan["objects"]:
        key = object_key(item["id"])
        head = storage.head(key)
        if plan["refresh"] and (not head or head.get("Metadata", {}).get("sha256") != item["sha256"]):
            _upload_chart(storage, item["id"], (plan_path.parent / "payload" / key).read_bytes(), refresh=True)
            completed.append(item["id"])
            continue
        if head:
            storage.verify(key, item["size"], item["sha256"], head["ETag"])
            if (head.get("Metadata", {}).get("sha256") != item["sha256"] or head.get("ContentType") != "application/json"
                    or head.get("CacheControl") != "no-cache,no-store,must-revalidate"):
                etag = storage.copy(key, head["ETag"], key, item["sha256"], "application/json", immutable=False)
                storage.verify(key, item["size"], item["sha256"], etag)
        else:
            _upload_chart(storage, item["id"], (plan_path.parent / "payload" / key).read_bytes())
        completed.append(item["id"])
    return {"completed": completed, "library": _publish_library(storage, plan["expected"])}


def publish(*, execute=False, refresh=False, output=Path("work/dxtag")):
    storage = Storage("dxtag")
    response = _fetch(SONG_LIST_URL)
    expected = expected_chart_ids(response.json())
    missing = missing_chart_ids(storage, expected)
    targets = sorted(expected) if refresh else missing
    result = {"missing": missing, "targets": targets, "uploaded": [], "failed": [], "library": None}
    if not execute:
        return result
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    prepared = {"bucket": storage.bucket, "expected": sorted(expected), "missing": missing, "refresh": refresh, "objects": []}
    for chart_id in targets:
        try:
            body = score_chart(chart_id, output)
        except (OSError, ValueError, RuntimeError, requests.RequestException, json.JSONDecodeError) as error:
            result["failed"].append({"id": chart_id, "stage": "prepare", "error": str(error)[:500]})
            continue
        path = output / "payload" / object_key(chart_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        prepared["objects"].append({"id": chart_id, "size": len(body), "sha256": digest(body)})
        try:
            if _upload_chart(storage, chart_id, body, refresh=refresh):
                result["uploaded"].append(chart_id)
        except Exception as error:
            stage = "upload" if recoverable_upload_error(error) else "verify"
            result["failed"].append({"id": chart_id, "stage": stage, "error": str(error)[:500]})
    (output / "plan.json").write_bytes(encode(prepared))
    if result["failed"]:
        raise DxtagIncomplete(result)
    try:
        result["library"] = _publish_library(storage, expected)
    except Exception as error:
        if not recoverable_upload_error(error):
            raise
        result["failed"].append({"id": None, "stage": "upload", "error": str(error)[:500]})
        raise DxtagIncomplete(result) from error
    return result
