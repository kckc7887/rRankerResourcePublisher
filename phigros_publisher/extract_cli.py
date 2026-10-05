"""Publisher-owned extraction entrypoint with chart extraction enabled."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

if __package__:
    from .parallel import checked_workers
else:
    from parallel import checked_workers


def _publish_extract_config(music: bool = False) -> dict:
    """Match Phigros_Resource defaults: charts on, music off unless requested."""
    return {
        "avatar": True,
        "chart": True,
        "illustrationBlur": True,
        "illustrationLowRes": True,
        "illustration": True,
        "music": bool(music),
        "UPDATE": {
            "main_story": 0,
            "side_story": 0,
            "other_song": 0,
        },
    }


def _ensure_output_dirs(output_root: Path, config: dict) -> dict[str, Path]:
    dirs = {
        "avatar": output_root / "avatar",
        "chart": output_root / "chart",
        "illustrationBlur": output_root / "illustrationBlur",
        "illustrationLowRes": output_root / "illustrationLowRes",
        "illustration": output_root / "illustration",
        "music": output_root / "music",
    }
    for key, path in dirs.items():
        if config.get(key):
            path.mkdir(parents=True, exist_ok=True)
    metadata_dir = output_root / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    return dirs


def preflight_audio() -> None:
    if not shutil.which("ffprobe"):
        raise RuntimeError("音乐格式校验需要 ffprobe，请安装 FFmpeg 并加入 PATH")
    try:
        from fsb5 import vorbis
        for library, symbol in ((vorbis.ogg, "ogg_stream_init"),
                                (vorbis.vorbis, "vorbis_info_init"),
                                (vorbis.vorbisenc, "vorbis_encode_setup_init")):
            getattr(library, symbol)
    except Exception as error:
        raise RuntimeError("音乐重建依赖不可用；Linux 需安装 libogg0、libvorbis0a、libvorbisenc2") from error


def run_extract(script_dir: Path, apk_path: Path, music: bool = False, workers: int = 4) -> None:
    checked_workers(workers)
    os.chdir(script_dir)
    sys.path.insert(0, str(script_dir))

    from gameInformation import run as extract_metadata
    from log import init_console_logger
    from resource import run as extract_resources

    if music:
        preflight_audio()
    config = _publish_extract_config(music=music)
    output_root = script_dir.parent / "output"
    metadata_dir = output_root / "metadata"
    output_dirs = _ensure_output_dirs(output_root, config)
    logger = init_console_logger()

    print("[*] 提取元数据...", flush=True)
    extract_metadata(str(apk_path), logger, str(metadata_dir))

    action = "提取媒体资源（含 chart、music）" if music else "提取媒体资源（含 chart）"
    print(f"[*] {action}...", flush=True)
    extract_resources(str(apk_path), config, logger, str(metadata_dir),
                      {key: str(path) for key, path in output_dirs.items()}, workers=workers)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phigros publisher extraction with charts enabled")
    parser.add_argument("script_dir", type=Path, help="phiTool script-py directory")
    parser.add_argument("apk_path", type=Path, help="APK file path")
    parser.add_argument("--workers", type=int, default=4, help="Parallel resource workers (1-16)")
    args = parser.parse_args()
    if not args.script_dir.is_dir():
        raise SystemExit(f"script_dir 不存在：{args.script_dir}")
    if not args.apk_path.is_file():
        raise SystemExit(f"APK 不存在：{args.apk_path}")
    run_extract(args.script_dir.resolve(), args.apk_path.resolve(), workers=args.workers)


if __name__ == "__main__":
    main()
