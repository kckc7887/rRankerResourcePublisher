from __future__ import annotations

import hashlib
from http.client import HTTPSConnection
import json
import os
import random
import string
import time
from typing import Any, Callable
import urllib.parse
import uuid

import requests


TAPTAP_HOST = "api.taptapdada.com"
PHIGROS_APP_ID = 165287
_SIGNING_SUFFIX = "PeCkE6Fu0B10Vm9BKfPfANwCUAn5POcs"
_NONCE_ALPHABET = string.ascii_lowercase + string.digits


def _download_session() -> requests.Session:
    session = requests.Session()
    # 旧 POC 可能在 Windows 环境中留下已失效的 HTTP(S)_PROXY。
    # 默认直连；确有代理需求时显式设置 PHIGROS_USE_ENV_PROXY=1。
    session.trust_env = os.environ.get("PHIGROS_USE_ENV_PROXY") == "1"
    return session


def get_latest_download(app_id: int = PHIGROS_APP_ID) -> dict[str, Any]:
    """Resolve the latest Phigros APK using the verified reference implementation."""
    uid = uuid.uuid4()
    x_ua = (
        "V=1&PN=TapTap&VN=2.40.1-rel.100000&VN_CODE=240011000&LOC=CN&"
        f"LANG=zh_CN&CH=default&UID={uid}&NT=1&SR=1080x2030&DEB=Xiaomi&"
        "DEM=Redmi+Note+5&OSV=9"
    )
    connection = HTTPSConnection(TAPTAP_HOST, timeout=30)
    try:
        detail_path = f"/app/v2/detail-by-id/{app_id}?X-UA={urllib.parse.quote(x_ua)}"
        connection.request("GET", detail_path, headers={"User-Agent": "okhttp/3.12.1"})
        detail_response = connection.getresponse()
        if detail_response.status != 200:
            raise RuntimeError(f"TapTap 详情接口返回 HTTP {detail_response.status}")
        detail = json.load(detail_response)
        download = detail["data"]["download"]
        apk_id = download["apk_id"]
        version_name = download["apk"]["version_name"]

        nonce = "".join(random.sample(_NONCE_ALPHABET, 5))
        timestamp = int(time.time())
        params = (
            "abi=arm64-v8a,armeabi-v7a,armeabi"
            f"&id={apk_id}&node={uid}&nonce={nonce}&sandbox=1"
            f"&screen_densities=xhdpi&time={timestamp}"
        )
        signature_source = f"X-UA={x_ua}&{params}{_SIGNING_SUFFIX}"
        signature = hashlib.md5(signature_source.encode()).hexdigest()
        body = f"{params}&sign={signature}".encode()
        endpoint = "/apk/v1/detail?X-UA=" + urllib.parse.quote(x_ua)
        connection.request(
            "POST",
            endpoint,
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "okhttp/3.12.1",
            },
        )
        apk_response = connection.getresponse()
        if apk_response.status != 200:
            raise RuntimeError(f"TapTap APK 接口返回 HTTP {apk_response.status}")
        apk = json.load(apk_response)["data"]["apk"]
        return {
            "url": apk["download"],
            "version": version_name,
            "apk_name": apk["name"],
            "size": int(apk["size"]),
        }
    finally:
        connection.close()


def probe_download(url: str) -> dict[str, Any]:
    """Open the download as a stream and pass only when the origin returns HTTP 200."""
    with _download_session() as session:
        with session.get(url, stream=True, timeout=(30, 60), allow_redirects=True) as response:
            status = response.status_code
            result = {
                "status": status,
                "final_url": response.url,
                "content_length": int(response.headers.get("content-length", "0") or 0),
                "content_type": response.headers.get("content-type", ""),
            }
            if status != 200:
                raise RuntimeError(f"最新 APK 下载地址返回 HTTP {status}，预期为 200")
            return result


def download_apk(
    info: dict[str, Any],
    destination: str,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    expected_size = int(info.get("size") or 0)
    downloaded = 0
    with _download_session() as session:
        with session.get(info["url"], stream=True, timeout=(30, 300)) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", expected_size) or expected_size)
            with open(destination, "wb") as output:
                for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
    if expected_size and downloaded != expected_size:
        raise RuntimeError(f"APK 大小不匹配：下载 {downloaded}，预期 {expected_size}")
    return destination
