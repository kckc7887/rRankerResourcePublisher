from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .parallel import bounded_map

# 官方谱面 JSON：type 1=Tap, 2=Drag, 3=Hold, 4=Flick
# 物量表数组顺序：[Tap, Hold, Drag, Flick]
NOTE_TYPE_TO_INDEX = {1: 0, 3: 1, 2: 2, 4: 3}
DIFFICULTY_FILES = ("EZ", "HD", "IN", "AT")


def count_chart_notes(chart: dict[str, Any]) -> list[int]:
    counts = [0, 0, 0, 0]
    for line in chart.get("judgeLineList", []) or []:
        if not isinstance(line, dict):
            continue
        for key in ("notesAbove", "notesBelow"):
            for note in line.get(key, []) or []:
                if not isinstance(note, dict):
                    continue
                index = NOTE_TYPE_TO_INDEX.get(note.get("type"))
                if index is None:
                    continue
                counts[index] += 1
    return counts


def _format_counts(counts: list[int]) -> str:
    return json.dumps(counts, separators=(",", ":"))


def _song_order(metadata_dir: Path, charts_dir: Path) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    info_path = metadata_dir / "info.tsv"
    if info_path.is_file():
        with info_path.open("r", encoding="utf-8", newline="") as source:
            for row in csv.reader(source, delimiter="\t"):
                if row and row[0] not in seen:
                    ordered.append(row[0])
                    seen.add(row[0])
    for song_dir in sorted((path for path in charts_dir.iterdir() if path.is_dir()), key=lambda path: path.name):
        if song_dir.name not in seen:
            ordered.append(song_dir.name)
            seen.add(song_dir.name)
    return ordered


def build_note_counts_rows(charts_dir: Path, metadata_dir: Path | None = None, workers: int = 4) -> list[list[str]]:
    if not charts_dir.is_dir():
        return []

    metadata_dir = metadata_dir or charts_dir.parent / "metadata"
    chart_paths: list[Path] = []
    song_charts: list[tuple[str, list[Path]]] = []
    for song_id in _song_order(metadata_dir, charts_dir):
        song_dir = charts_dir / song_id
        if not song_dir.is_dir():
            continue
        paths: list[Path] = []
        for difficulty in DIFFICULTY_FILES:
            chart_path = song_dir / f"{difficulty}.json"
            if not chart_path.is_file():
                break
            paths.append(chart_path)
        if paths:
            song_charts.append((song_id, paths))
            chart_paths.extend(paths)

    def count(path: Path) -> str:
        try:
            return _format_counts(count_chart_notes(json.loads(path.read_text(encoding="utf-8"))))
        except Exception as error:
            raise ValueError(f"谱面物量统计失败：{path}") from error

    counts = dict(zip(chart_paths, bounded_map(count, chart_paths, workers)))
    return [[song_id, *(counts[path] for path in paths)] for song_id, paths in song_charts]


def write_note_counts_tsv(
    charts_dir: Path,
    output_path: Path,
    metadata_dir: Path | None = None,
    workers: int = 4,
) -> dict[str, Any]:
    rows = build_note_counts_rows(charts_dir, metadata_dir, workers=workers)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as sink:
        writer = csv.writer(sink, delimiter="\t", lineterminator="\n")
        for row in rows:
            writer.writerow(row)

    difficulty_histogram = {3: 0, 4: 0}
    for row in rows:
        difficulty_histogram[len(row) - 1] = difficulty_histogram.get(len(row) - 1, 0) + 1

    return {
        "path": str(output_path),
        "song_count": len(rows),
        "three_difficulty_songs": difficulty_histogram.get(3, 0),
        "four_difficulty_songs": difficulty_histogram.get(4, 0),
    }
