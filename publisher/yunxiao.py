from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import time
import zipfile

import requests

from .storage import BUCKETS, _upload_progress, encode, file_digest, safe_path

REPOSITORY = "kckc7887/rRankerResourcePublisher"
PIPELINES = {"apk": "APK", "kyou": "PHIGROS", "phigros": "PHIGROS", "rizline": "RIZLINE", "dxtag": "MAIMAI"}


def github(path, token=None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(f"https://api.github.com/repos/{REPOSITORY}/{path}", headers=headers, timeout=30)
    if not response.ok:
        raise RuntimeError(f"GitHub publication request failed: HTTP {response.status_code}")
    return response.json()


def source_workflow(game, run_id, sha, attempt=None, token=None):
    run = github(f"actions/runs/{run_id}", token)
    path = f".github/workflows/{game}.yml"
    if (run["head_branch"] != "master" or run["head_sha"] != sha or run["path"] != path
            or run["event"] not in ("schedule", "workflow_dispatch") or run["status"] != "in_progress"
            or (attempt is not None and not 0 < attempt <= run["run_attempt"])):
        raise ValueError("Source publication is not the active master workflow")
    return f"{REPOSITORY}/{path}@refs/heads/master"


def dispatch(game, *, release_id=None, attempt=None, timeout=85 * 60):
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/master":
        raise ValueError("Cloud publication requires the publisher master workflow")
    sha = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid publisher commit")
    endpoint = os.environ["YUNXIAO_ENDPOINT"].rstrip("/")
    if not endpoint.startswith("https://"):
        raise ValueError("Yunxiao endpoint must use HTTPS")
    pipeline = os.environ[f"YUNXIAO_{PIPELINES[game]}_PIPELINE_ID"]
    token = os.environ["YUNXIAO_TOKEN"]
    if not token or not pipeline:
        raise ValueError("Missing Yunxiao pipeline credentials")
    values = {"GAME": game, "PUBLISHER_SHA": sha, "SOURCE_RUN_ID": str(int(os.environ["GITHUB_RUN_ID"]))}
    if game == "apk":
        if release_id is None or release_id <= 0:
            raise ValueError("A formal Release ID is required")
        values["RELEASE_ID"] = str(release_id)
    else:
        if attempt is None or attempt <= 0:
            raise ValueError("The prepared publication attempt is required")
        values["PUBLICATION_ATTEMPT"] = str(attempt)
    base = f"{endpoint}/oapi/v1/flow/pipelines/{pipeline}/runs"
    headers = {"x-yunxiao-token": token}
    response = requests.post(base, headers=headers, json={"params": json.dumps({
        "envs": values, "comment": f"GitHub {game} run {values['SOURCE_RUN_ID']}",
    })}, timeout=30)
    if not response.ok:
        raise RuntimeError(f"Yunxiao dispatch failed: HTTP {response.status_code}")
    run_id = response.json()
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("Yunxiao did not return a run ID")
    print(f"Yunxiao run {run_id}: {endpoint}/flow/pipelines/{pipeline}/current", flush=True)
    url = f"{base}/{run_id}"
    terminal = False
    previous = None
    deadline = time.monotonic() + timeout
    def terminate(signum, frame):
        raise KeyboardInterrupt("GitHub job cancelled")
    old_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        while time.monotonic() < deadline:
            response = requests.get(url, headers=headers, timeout=30)
            if not response.ok:
                raise RuntimeError(f"Yunxiao status request failed: HTTP {response.status_code}")
            status = response.json()["status"]
            if status != previous:
                print(f"Yunxiao run {run_id}: {status}", flush=True)
                previous = status
            if status == "SUCCESS":
                terminal = True
                return run_id
            if status not in ("RUNNING", "WAITING"):
                terminal = True
                raise RuntimeError(f"Yunxiao publication {status}")
            time.sleep(30)
        raise TimeoutError("Yunxiao publication timed out")
    except BaseException:
        if not terminal:
            try:
                stopped = requests.put(url, headers=headers, timeout=30)
                if not stopped.ok or stopped.json() is not True:
                    raise RuntimeError("stop was not acknowledged")
                print(f"Stopped Yunxiao run {run_id}", file=sys.stderr, flush=True)
            except Exception:
                print(f"Could not stop Yunxiao run {run_id}; inspect the pipeline", file=sys.stderr, flush=True)
        raise
    finally:
        signal.signal(signal.SIGTERM, old_handler)


def upload(game, run_id, attempt, output):
    from .dxtag import publish_prepared
    from .publication import finalize, garbage_collect, upload_shard

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    token = os.environ["GITHUB_ARTIFACT_TOKEN"]
    sha = os.environ["PUBLISHER_SHA"]
    if os.environ["S3_BUCKET_NAME"] != BUCKETS[game]:
        raise ValueError("Cloud publication bucket changed")
    api = f"https://api.github.com/repos/{REPOSITORY}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
    source_workflow(game, run_id, sha, attempt, token)
    artifacts = []
    page = 1
    while True:
        rows = github(f"actions/runs/{run_id}/artifacts?per_page=100&page={page}", token)["artifacts"]
        artifacts.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    def extract(name, destination):
        matches = [row for row in artifacts if row["name"] == name and not row["expired"]]
        if len(matches) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", matches[0].get("digest", "")):
            raise ValueError(f"Missing or unverifiable publication artifact: {name}")
        artifact = matches[0]
        response = requests.get(f"{api}/actions/artifacts/{artifact['id']}/zip", headers=headers, allow_redirects=False, timeout=30)
        if response.status_code != 302:
            raise RuntimeError(f"GitHub artifact authorization failed: HTTP {response.status_code}")
        location = response.headers["Location"]
        if not location.startswith("https://"):
            raise ValueError("Artifact download must use HTTPS")
        archive = output / f"{artifact['id']}.zip"
        progress = _upload_progress("Artifact download", name, artifact["size_in_bytes"])
        count = 0
        with requests.get(location, stream=True, timeout=(30, 300)) as download:
            if not download.ok:
                raise RuntimeError(f"Artifact download failed: HTTP {download.status_code}")
            with archive.open("wb") as target:
                for chunk in download.iter_content(1024 * 1024):
                    target.write(chunk)
                    count += len(chunk)
                    progress(count)
        if f"sha256:{file_digest(archive)}" != artifact["digest"]:
            raise ValueError(f"Publication artifact checksum mismatch: {name}")
        with zipfile.ZipFile(archive) as compressed:
            for item in compressed.infolist():
                safe_path(item.filename.rstrip("/"))
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("Publication artifact contains a symbolic link")
            compressed.extractall(destination)
        archive.unlink()
    if game == "dxtag":
        extract(f"dxtag-prepared-{attempt}", output / "prepared")
        source_workflow(game, run_id, sha, attempt, token)
        return publish_prepared(output / "prepared/plan.json")
    extract(f"{game}-plan-{attempt}", output / "publication")
    plan = json.loads((output / "publication/plan.json").read_bytes())
    if plan["game"] != game or plan["bucket"] != BUCKETS[game]:
        raise ValueError("Publication artifact targets another resource group")
    receipts = []
    for shard in plan["shards"]:
        payload = output / f"shard-{shard['id']}"
        extract(f"{game}-shard-{shard['id']}-{attempt}", payload)
        source_workflow(game, run_id, sha, attempt, token)
        receipts.append(upload_shard(plan, shard["id"], payload, output / f"receipt-{shard['id']}.json"))
        shutil.rmtree(payload)
    workflow = source_workflow(game, run_id, sha, attempt, token)
    result = finalize(plan, receipts, github_workflow=workflow)
    garbage_collect(game, execute=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    dispatch_command = commands.add_parser("dispatch")
    dispatch_command.add_argument("game", choices=PIPELINES)
    dispatch_command.add_argument("--release-id", type=int)
    dispatch_command.add_argument("--attempt", type=int)
    dispatch_command.add_argument("--timeout-minutes", type=int, default=85)
    upload_command = commands.add_parser("upload")
    upload_command.add_argument("game", choices=("kyou", "phigros", "rizline", "dxtag"))
    upload_command.add_argument("--run-id", type=int, required=True)
    upload_command.add_argument("--attempt", type=int, required=True)
    upload_command.add_argument("--output", type=Path, default=Path("work/cloud"))
    args = parser.parse_args()
    if args.command == "dispatch":
        result = {"runId": dispatch(args.game, release_id=args.release_id, attempt=args.attempt, timeout=args.timeout_minutes * 60)}
    else:
        result = upload(args.game, args.run_id, args.attempt, args.output)
    print(encode(result).decode(), end="")


if __name__ == "__main__":
    main()
