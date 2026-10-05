from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

import requests

from .storage import Storage, encode, file_digest

REPOSITORY = "kckc7887/rRanker"
ABI_NAMES = {"arm64-v8a": "arm64", "armeabi-v7a": "armeabi", "x86": "x86", "x86_64": "x86_64"}
CERTIFICATE = "fac61745dc0903786fb9ede62a962b399f7348f0bb6f899b8332667591033b9c"


def github(path):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(f"https://api.github.com/repos/{REPOSITORY}/{path}", headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def formal_release(release_id):
    release = github(f"releases/{int(release_id)}")
    if release["id"] != int(release_id) or release["draft"] or release["prerelease"] or not release.get("published_at"):
        raise ValueError("Only a published formal release may be mirrored")
    latest = github("releases/latest")
    if latest["id"] != release["id"]:
        raise ValueError("Refusing to overwrite the latest APKs with an older Release")
    return release


def verify_apk(path, abi, version):
    with zipfile.ZipFile(path) as archive:
        native = {name.split("/")[1] for name in archive.namelist() if name.startswith("lib/") and name.endswith(".so")}
        if native != {abi} or "AndroidManifest.xml" not in archive.namelist():
            raise ValueError(f"APK ABI mismatch: {path.name}")
        if archive.testzip() is not None:
            raise ValueError(f"Corrupt APK: {path.name}")
    sdk = Path(os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT", "")))
    versions = [directory for directory in (sdk / "build-tools").iterdir() if re.fullmatch(r"\d+\.\d+\.\d+", directory.name)]
    tools = max(versions, key=lambda directory: tuple(map(int, directory.name.split("."))))
    def run(tool, *args):
        executable = shutil.which(tool, path=str(tools)) or str(tools / tool)
        return subprocess.check_output([executable, *args, str(path)], text=True, encoding="utf-8", timeout=60)
    badging = run("aapt", "dump", "badging")
    package = next(line for line in badging.splitlines() if line.startswith("package:"))
    fields = dict(re.findall(r"(\w+)='([^']*)'", package))
    if fields.get("name") != "com.rranker.app" or fields.get("versionName") != version:
        raise ValueError(f"APK package/version mismatch: {path.name}")
    signature = run("apksigner", "verify", "--verbose", "--print-certs")
    certificates = []
    for line in signature.splitlines():
        line = line.strip()
        if " certificate SHA-256 digest:" not in line or line.startswith("Source Stamp Signer"):
            continue
        match = re.fullmatch(r"(?:Signer #\d+|V(?:1|2|3\.0) Signer:) certificate SHA-256 digest: ([0-9a-fA-F:]+)", line)
        if not match:
            raise ValueError(f"Unrecognized APK signing certificate summary: {path.name}")
        certificates.append(match[1])
    if len(certificates) != 1 or certificates[0].replace(":", "").lower() != CERTIFICATE:
        raise ValueError(f"APK signing certificate mismatch: {path.name}")
    return int(fields["versionCode"])


def publish(release_id, output, *, bootstrap=False, execute=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report = {"releaseId": int(release_id), "objects": [], "completed": []}
    try:
        release = formal_release(release_id)
        report.update(tag=release["tag_name"], publishedAt=release["published_at"])
        version = release["tag_name"].removeprefix("v")
        if bootstrap and version != "0.3.0":
            raise ValueError("Bootstrap only adopts the existing 0.3.0 objects")
        assets = [asset for asset in release["assets"] if asset["name"].endswith(".apk")]
        expected_names = {f"rRanker-{'0.3.0-' if bootstrap else ''}{name}.apk" for name in ABI_NAMES.values()}
        if len(assets) != 4 or {asset["name"] for asset in assets} != expected_names or any(asset["state"] != "uploaded" for asset in assets):
            raise ValueError("Release must contain exactly the four expected APK attachments")
        storage = Storage("apk")
        version_codes = set()
        for abi, name in ABI_NAMES.items():
            asset_name = f"rRanker-{'0.3.0-' if bootstrap else ''}{name}.apk"
            asset = next(item for item in assets if item["name"] == asset_name)
            target = f"release/rRanker-{name}.apk"
            path = output / Path(target).name
            source = None
            if bootstrap:
                source_key = f"release/0.3.0/{asset_name}"
                data, etag = storage.get(source_key)
                source = {"key": source_key, "etag": etag}
                path.write_bytes(data)
            else:
                with requests.get(asset["browser_download_url"], stream=True, timeout=(30, 300)) as response:
                    response.raise_for_status()
                    with path.open("wb") as file:
                        for chunk in response.iter_content(4 * 1024 * 1024):
                            file.write(chunk)
            sha = file_digest(path)
            if path.stat().st_size != asset["size"] or (asset.get("digest") and asset["digest"] != f"sha256:{sha}"):
                raise ValueError(f"Release attachment checksum/size mismatch: {asset_name}")
            version_codes.add(verify_apk(path, abi, version))
            report["objects"].append({"key": target, "size": asset["size"], "sha256": sha, "abi": abi, "source": source})
        if len(version_codes) != 1:
            raise ValueError("APK version codes differ")
        report["versionCode"] = version_codes.pop()
        if execute:
            for item in report["objects"]:
                formal_release(release_id)
                head = storage.head(item["key"])
                if head and head["ContentLength"] == item["size"] and head.get("Metadata", {}).get("sha256") == item["sha256"]:
                    storage.verify(item["key"], item["size"], item["sha256"], head["ETag"])
                else:
                    if bootstrap:
                        etag = storage.copy(item["source"]["key"], item["source"]["etag"], item["key"], item["sha256"], "application/vnd.android.package-archive", immutable=False)
                    else:
                        with (output / Path(item["key"]).name).open("rb") as file:
                            etag = storage.put(item["key"], file, "application/vnd.android.package-archive", sha256=item["sha256"],
                                               etag=head["ETag"] if head else None, absent=not head)
                    storage.verify(item["key"], item["size"], item["sha256"], etag)
                report["completed"].append(item["key"])
        report["status"] = "published" if execute else "verified"
        return report
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = str(error)
        raise
    finally:
        (output / "report.json").write_bytes(encode(report))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release_id", type=int)
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("work/apk"))
    args = parser.parse_args()
    print(json.dumps(publish(args.release_id, args.output, bootstrap=args.bootstrap, execute=args.execute), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
