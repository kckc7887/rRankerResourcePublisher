from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def build(game, output):
    root = Path(__file__).resolve().parent.parent
    work = Path("work") / game
    work.mkdir(parents=True, exist_ok=True)
    output = Path(output).resolve()
    if game == "rizline":
        from rizline_publisher.core import build as build_rizline
        from rizline_publisher.upstream import import_catalog
        overrides = root / "rizline_publisher/overrides.json"
        import_catalog(work, Path(".cache/http"), overrides, "urllib", 4)
        build_rizline(work / "catalog.json", overrides, output, workers=4)
    elif game == "phigros":
        from phigros_publisher.extract_cli import run_extract
        from phigros_publisher.organizer import organize_release, validate_release
        from phigros_publisher.taptap import download_apk, get_latest_download
        info = get_latest_download()
        apk = (work / "game.apk").resolve()
        download_apk(info, str(apk))
        toolchain = (work / "phiTool").resolve()
        if toolchain.exists():
            if not toolchain.is_relative_to(work.resolve()):
                raise ValueError("Toolchain output outside workspace")
            shutil.rmtree(toolchain)
        shutil.copytree(root / "bundled/phiTool", toolchain)
        original_cwd, original_path = Path.cwd(), list(sys.path)
        try:
            run_extract(toolchain / "script-py", apk, music=True, workers=4)
        finally:
            os.chdir(original_cwd)
            sys.path[:] = original_path
        release = organize_release(toolchain / "output", output, info["version"], workers=4)
        validate_release(release)
    else:
        subprocess.run([sys.executable, "-m", "kyou_publisher.crawler", "--headful", "--out", str(output)], check=True)
