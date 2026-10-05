import base64
import copy
import hashlib
import io
import json
import os
import struct
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from rizline_publisher.core import apply_overrides, atomic_write, build, json_bytes, max_combo, read_json, relative_path, sha256, supplement_template, validate_catalog, validate_release
from rizline_publisher.upstream import Addressables, attach_achievements, chart_stats, parse_stats, verified_stats
from rizline_publisher.audio import acb_duration, utf_rows


def fixture():
    song = {"id": "Song.artist.0", "title": "Song", "artist": "Artist", "illustrator": None, "packId": "Disc 1", "packName": "Disc 1", "bpm": "150", "durationSeconds": None, "updatedAt": None, "coverPath": "covers/test.png", "audioPath": "audio/test.acb", "charts": [{"id": "chart.Song.artist.0.IN", "songId": "Song.artist.0", "difficulty": "IN", "level": "12+", "constant": 12.6, "designer": "Designer", "hit": 20, "combo": 56, "maxScore": 1001000, "riztimeHit": 10, "chartPath": "charts/test.json"}], "achievements": []}
    return {"schemaVersion": 1, "resourceVersion": "v141_example", "gameVersion": "2.7.1", "songs": [song]}


def overrides():
    return {"schemaVersion": 1, "songs": {}, "charts": {}, "statAliases": {}, "achievementSongs": {}}


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH9sAAAAASUVORK5CYII=")
CHART_JSON = json_bytes({"bPM": 150, "lines": []})
SAMPLE_M4A = b"\x00\x00\x00\x18ftypM4A " + bytes(64)


def fake_transcode(acb, cache_dir, expected_duration):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    return SAMPLE_M4A


def cri_utf_table(fields):
    strings, binary, schema, row = b"Header\0", b"", b"", b""
    for name, value in fields.items():
        name_offset = len(strings)
        strings += name.encode() + b"\0"
        kind = 11 if isinstance(value, bytes) else 4
        schema += bytes([0x50 | kind]) + struct.pack(">I", name_offset)
        if kind == 11:
            row += struct.pack(">II", len(binary), len(value))
            binary += value
        else:
            row += struct.pack(">I", value)
    rows_at = 32 + len(schema)
    strings_at = rows_at + len(row)
    binary_at = strings_at + len(strings)
    size = binary_at + len(binary)
    header = b"@UTF" + struct.pack(">IHHIIIHHI", size - 8, 1, rows_at - 8, strings_at - 8, binary_at - 8, 0, len(fields), len(row), 1)
    return header + schema + row + strings + binary


def sample_acb(samples=1720):
    hca = b"HCA\0" + struct.pack(">HH", 0x300, 32) + b"fmt\0" + b"\2" + (44100).to_bytes(3, "big") + struct.pack(">IHH", 2, 128, 200) + b"comp" + struct.pack(">H", 10) + b"\0\0" + bytes(20)
    bank = b"AFS2" + bytes([2, 4]) + struct.pack("<HIHH", 4, 1, 32, 0) + struct.pack("<III", 0, 28, 32 + len(hca)) + bytes(4) + hca
    return cri_utf_table({"WaveformTable": cri_utf_table({"NumSamples": samples, "SamplingRate": 44100}), "AwbFile": bank})


def write_fixture_assets(root):
    atomic_write(root / "covers/test.png", PNG)
    atomic_write(root / "audio/test.acb", sample_acb())
    atomic_write(root / "charts/test.json", CHART_JSON)




class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.override, self.output = self.root / "work/catalog.json", self.root / "overrides.json", self.root / "dist"
        atomic_write(self.source, json_bytes(fixture()))
        # Local PNG/ACB/JSON fixtures; no network is needed for tests.
        write_fixture_assets(self.source.parent)
        atomic_write(self.override, json_bytes(overrides()))
        self.transcode = patch("rizline_publisher.transcode.transcode_acb", side_effect=fake_transcode)
        self.transcode.start()
        self.addCleanup(self.transcode.stop)

    def tearDown(self):
        self.temp.cleanup()


    def test_build_is_deterministic_preserves_input_and_override(self):
        patch_value = overrides()
        patch_value["songs"]["Song.artist.0"] = {"updatedAt": "2026-09-11", "durationSeconds": 123.456}
        atomic_write(self.override, json_bytes(patch_value))
        before = self.override.read_bytes(), self.source.read_bytes()
        first = build(self.source, self.override, self.output)
        pointer = (self.output / "rizline/current.json").read_bytes()
        self.assertEqual(first, build(self.source, self.override, self.output))
        self.assertEqual(pointer, (self.output / "rizline/current.json").read_bytes())
        self.assertEqual(before, (self.override.read_bytes(), self.source.read_bytes()))
        self.assertEqual(first["missingUpdateDates"], 0)
        current = read_json(self.output / "rizline/current.json")
        manifest = read_json(self.output / current["manifestPath"])
        catalog = read_json(self.output / manifest["catalogPath"])
        paths = {asset["path"] for asset in manifest["files"]}
        song = catalog["songs"][0]
        self.assertTrue(song["audioPath"].endswith(".m4a") and song["audioPath"] in paths)
        self.assertTrue(song["charts"][0]["chartPath"].endswith(".json") and song["charts"][0]["chartPath"] in paths)
        self.assertEqual((self.output / song["audioPath"]).read_bytes(), SAMPLE_M4A)
        self.assertEqual((self.output / song["charts"][0]["chartPath"]).read_bytes(), CHART_JSON)
        template = read_json(self.source.parent / "supplement-template.json")
        self.assertNotIn("updatedAt", template["songs"]["Song.artist.0"])

    def test_supplement_template_lists_full_ids_and_only_missing_fields(self):
        catalog = fixture()
        special = copy.deepcopy(catalog["songs"][0])
        special["id"] = "Song.artist.1"
        special["charts"][0].update(id="chart.Song.artist.1.SP", songId=special["id"], difficulty="SP", level="竹", constant=None, maxScore=None, riztimeHit=None)
        catalog["songs"].append(special)
        template = supplement_template(catalog)
        self.assertEqual(template["songs"]["Song.artist.0"], {"illustrator": None, "durationSeconds": None, "updatedAt": None})
        self.assertEqual(template["charts"], {"chart.Song.artist.1.SP": {"maxScore": None, "riztimeHit": None}})
        self.assertNotIn("constant", template["charts"]["chart.Song.artist.1.SP"])
        self.assertEqual(set(template), set(overrides()))

    def test_override_edit_creates_new_release(self):
        first = build(self.source, self.override, self.output)
        patched = overrides()
        patched["charts"]["chart.Song.artist.0.IN"] = {"designer": "Correct designer"}
        atomic_write(self.override, json_bytes(patched))
        second = build(self.source, self.override, self.output)
        self.assertNotEqual(first["resourceVersion"], second["resourceVersion"])
        self.assertTrue((self.output / "rizline/releases" / first["resourceVersion"] / "catalog.json").exists())

    def test_corruption_is_detected(self):
        build(self.source, self.override, self.output)
        current = read_json(self.output / "rizline/current.json")
        manifest = read_json(self.output / current["manifestPath"])
        (self.output / manifest["files"][0]["path"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "integrity"):
            validate_release(self.output)


    def test_paths_and_references_fail_closed(self):
        for path in ("../outside", "/absolute", "a\\b", "a//b", "a/./b", "https://example.com/x", "x?y"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_path(path)
        value = fixture()
        value["songs"][0]["charts"][0]["songId"] = "wrong"
        with self.assertRaisesRegex(ValueError, "mislinked"):
            validate_catalog(value)

    def test_sp_stays_independent_and_cannot_get_constant(self):
        value = fixture()
        special = copy.deepcopy(value["songs"][0])
        special["id"] = "Song.artist.1"
        special["charts"][0].update(id="chart.Song.artist.1.SP", songId=special["id"], difficulty="SP", level="竹", constant=None)
        value["songs"].append(special)
        self.assertEqual(validate_catalog(value)["songs"], 2)
        special["charts"][0]["constant"] = 12
        with self.assertRaisesRegex(ValueError, "SP"):
            validate_catalog(value)

    def test_unknown_override_id_does_not_silently_disappear(self):
        value = overrides()
        value["songs"]["typo"] = {"title": "Edited"}
        with self.assertRaisesRegex(ValueError, "Unknown override"):
            apply_overrides(fixture(), value)

    def test_audio_and_chart_paths_cannot_be_overridden(self):
        songs, charts = overrides(), overrides()
        songs["songs"]["Song.artist.0"] = {"audioPath": "audio/other.acb"}
        charts["charts"]["chart.Song.artist.0.IN"] = {"chartPath": "charts/other.json"}
        for value in (songs, charts):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Unsupported override"):
                apply_overrides(fixture(), value)






class ImportTests(unittest.TestCase):
    def test_combo_boundaries_and_hold_counts(self):
        self.assertEqual([max_combo(n) for n in (0, 5, 6, 8, 9, 11, 12, 537)], [0, 5, 7, 11, 14, 20, 24, 2124])
        self.assertEqual(chart_stats({"bPM": 150, "lines": [{"notes": [{"type": 0}, {"type": 1}, {"type": 2}]}]})[0], {"hit": 4, "combo": 4})

    def test_statistics_are_parsed_as_data_and_hit_must_match(self):
        value = parse_stats(b'let songAllData = [\n// comment\n{"name":"Song","IN":{"mHit":20,"mH":10}},\n];')
        chart = fixture()["songs"][0]["charts"][0]
        self.assertEqual(verified_stats(value[0], chart), {"riztimeHit": 10, "maxScore": 1001000})
        chart["hit"] = 21
        self.assertIsNone(verified_stats(value[0], chart))
        with self.assertRaises(ValueError):
            parse_stats(b'let songAllData = [process.exit()];')

    def test_achievements_do_not_attach_to_sp_by_same_title(self):
        songs = fixture()["songs"]
        special = copy.deepcopy(songs[0])
        special["id"] = "Song.artist.1"
        special["charts"][0]["difficulty"] = "SP"
        songs.append(special)
        attach_achievements(songs, {"ach.song.name": "First", "ach.song.desc": 'Play “Song” at 118%'}, overrides())
        self.assertEqual(len(songs[0]["achievements"]), 1)
        self.assertEqual(songs[1]["achievements"], [])

    def test_general_perfect_achievement_is_not_reported_as_unknown_song(self):
        songs = fixture()["songs"]
        unresolved = attach_achievements(songs, {"ach.any.name": "Any", "ach.any.desc": '游玩任意关卡并获得 “PERFECT” 评价'}, overrides())
        self.assertEqual(unresolved, [])
        self.assertEqual(songs[0]["achievements"], [])

    def test_addressables_handles_long_keys_and_multiple_dependencies(self):
        integer = lambda n: struct.pack("<i", n)
        key = "long-key-" + "x" * 300
        keys = b"\0" + integer(len(key)) + key.encode() + b"\4" + integer(7)
        buckets = integer(2) + integer(0) + integer(1) + integer(0) + integer(5 + len(key)) + integer(2) + integer(1) + integer(2)
        entry = lambda internal, dep: b"".join(integer(n) for n in (internal, 0, dep, 0, 0, 0, 0))
        entries = integer(3) + entry(0, 1) + entry(1, -1) + entry(2, -1)
        data = {"m_InternalIds": ["asset", "https://cdn/default/a.bundle", "https://cdn/default/b.bundle"], "m_KeyDataString": base64.b64encode(keys).decode(), "m_BucketDataString": base64.b64encode(buckets).decode(), "m_EntryDataString": base64.b64encode(entries).decode()}
        self.assertEqual(Addressables(data).bundles(key), data["m_InternalIds"][1:])

    def test_bpm_range_uses_actual_tempo_shifts(self):
        _, bpm = chart_stats({"bPM": 150, "bpmShifts": [{"value": 0.866667}, {"value": 2.1}], "lines": []})
        self.assertEqual(bpm, (130.0, 315.0))


class AudioTests(unittest.TestCase):
    def audio(self, samples=1720):
        return sample_acb(samples)

    def test_duration_uses_real_samples_excluding_codec_padding(self):
        self.assertAlmostEqual(acb_duration(self.audio()), 1720 / 44100, places=6)

    def test_audio_rejects_mismatch_and_truncated_metadata(self):
        with self.assertRaisesRegex(ValueError, "disagrees"):
            acb_duration(self.audio(samples=2048))
        with self.assertRaisesRegex(ValueError, "Invalid CRI UTF"):
            acb_duration(self.audio()[:-1])


if __name__ == "__main__":
    unittest.main()
