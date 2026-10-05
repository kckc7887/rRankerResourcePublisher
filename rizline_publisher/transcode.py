"""Decode imported ACB with vgmstream-cli and encode AAC/M4A with ffmpeg."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

CRI_HCA_KEY = "0"
AAC_BITRATE = "192k"
DURATION_TOLERANCE_SECONDS = 0.12
_M4A_BRANDS = {b"M4A ", b"M4B ", b"mp42", b"isom", b"iso2", b"mp41"}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def is_m4a(data):
    return isinstance(data, (bytes, bytearray)) and len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _M4A_BRANDS


def _digest_lock(digest):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(digest, threading.Lock())


def _tool(names, env_name):
    override = os.environ.get(env_name, "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return str(path)
        raise ValueError(f"{env_name} is not an executable file")
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise ValueError("Need " + " or ".join(names) + " on PATH")


def vgmstream_cli():
    return _tool(("vgmstream-cli", "vgmstream123"), "VGMSTREAM_CLI")


def ffmpeg_cli():
    return _tool(("ffmpeg",), "FFMPEG")


def ffprobe_cli():
    return _tool(("ffprobe",), "FFPROBE")


def _run(args, timeout):
    completed = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", "replace").strip()
        raise ValueError((detail or "command failed") + ": " + " ".join(args[:3]))
    return completed


def m4a_duration(path):
    completed = _run([ffprobe_cli(), "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=30)
    text = completed.stdout.decode("ascii", "replace").strip()
    try:
        seconds = float(text)
    except ValueError as error:
        raise ValueError("ffprobe did not return a duration") from error
    if not seconds > 0:
        raise ValueError("Transcoded audio duration is invalid")
    return seconds


def transcode_acb(acb, cache_dir, expected_duration):
    from .core import atomic_write
    if not isinstance(acb, (bytes, bytearray)) or not acb:
        raise ValueError("ACB payload is empty")
    if type(expected_duration) is not float and type(expected_duration) is not int:
        raise ValueError("Expected ACB duration is invalid")
    digest = hashlib.sha256(bytes(acb)).hexdigest()
    cache = Path(cache_dir)
    cached = cache / (digest + ".m4a")
    with _digest_lock(digest):
        if cached.exists():
            data = cached.read_bytes()
            if is_m4a(data):
                return data
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rizline-transcode-") as directory:
            work = Path(directory)
            acb_path, wav_path, m4a_path = work / "source.acb", work / "pcm.wav", work / "audio.m4a"
            acb_path.write_bytes(acb)
            _run([vgmstream_cli(), "-k", CRI_HCA_KEY, "-o", str(wav_path), str(acb_path)], timeout=180)
            if not wav_path.exists() or wav_path.stat().st_size < 44:
                raise ValueError("vgmstream-cli did not write a WAV")
            _run([ffmpeg_cli(), "-hide_banner", "-nostdin", "-y", "-i", str(wav_path),
                  "-c:a", "aac", "-b:a", AAC_BITRATE, "-movflags", "+faststart", "-vn", str(m4a_path)], timeout=180)
            data = m4a_path.read_bytes()
            if not is_m4a(data):
                raise ValueError("ffmpeg did not write AAC/M4A")
            encoded = m4a_duration(m4a_path)
            if abs(encoded - float(expected_duration)) > DURATION_TOLERANCE_SECONDS:
                raise ValueError(f"Transcoded duration {encoded} differs from ACB {expected_duration}")
        atomic_write(cached, data)
        return data
