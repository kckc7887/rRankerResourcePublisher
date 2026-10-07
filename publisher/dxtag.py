from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import requests
from botocore.exceptions import ClientError

from .storage import Storage, encode, parallel, safe_path

SONG_LIST_URL = "https://maimai.lxns.net/api/v0/maimai/song/list"
CHART_URL = "https://assets2.lxns.net/maimai/chart/{chart_id}.txt"
DIFFICULTY_IDS = {"BASIC": 0, "ADVANCED": 1, "EXPERT": 2, "MASTER": 3, "Re:MASTER": 4}
AXES = ("键盘", "星星", "技巧", "体力", "爆发")
SCORE_SCRIPT = Path(__file__).with_name("dxtag-score.mjs")
OBJECT_KEY = re.compile(r"DXTag/(0|[1-9][0-9]*)\.json")


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


def present_chart_ids(objects):
    return {int(match.group(1)) for key in objects if (match := OBJECT_KEY.fullmatch(key))}


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


def score_chart(chart_id, output):
    if not os.environ.get("DXTAG_ROOT"):
        raise ValueError("DXTAG_ROOT is required")
    response = requests.get(CHART_URL.format(chart_id=chart_id), timeout=(10, 60))
    response.raise_for_status()
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


def publish(*, execute=False, output=Path("work/dxtag")):
    storage = Storage("dxtag")
    response = requests.get(SONG_LIST_URL, timeout=(10, 60))
    response.raise_for_status()
    expected = expected_chart_ids(response.json())
    missing = sorted(expected - present_chart_ids(storage.list("DXTag/")))
    result = {"missing": missing, "uploaded": [], "failed": [], "library": None}
    if not execute:
        return result
    output.mkdir(parents=True, exist_ok=True)
    for chart_id in missing:
        try:
            storage.put(object_key(chart_id), score_chart(chart_id, output), "application/json", absent=True)
            result["uploaded"].append(chart_id)
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] == 412:
                continue
            result["failed"].append({"id": chart_id, "error": str(error)[:500]})
        except (OSError, ValueError, RuntimeError, requests.RequestException, json.JSONDecodeError) as error:
            result["failed"].append({"id": chart_id, "error": str(error)[:500]})
    if result["failed"]:
        raise DxtagIncomplete(result)
    library = dict(parallel(lambda chart_id: (str(chart_id), storage.json(object_key(chart_id))[0]), sorted(expected)))
    storage.put("DXTag/all.json", encode(library), "application/json")
    result["library"] = {"key": "DXTag/all.json", "charts": len(library)}
    return result
