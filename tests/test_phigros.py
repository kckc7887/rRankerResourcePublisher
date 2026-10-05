import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from phigros_publisher.organizer import organize_release, validate_release


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audio_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.audio_temp.cleanup)
        audio_path = Path(cls.audio_temp.name) / 'fixture.ogg'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                        '-t', '0.1', '-c:a', 'libvorbis', str(audio_path)], check=True, timeout=30)
        cls.audio = audio_path.read_bytes()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.extracted = self.root / 'extracted'
        files = {
            'metadata/info.tsv': 'Song.A\tSong\tArtist\tIllustrator\tCharter\n',
            'metadata/difficulty.tsv': 'Song.A\t1\n',
            'chart/Song.A.0/EZ.json': json.dumps({'judgeLineList': []}),
        }
        for name, content in files.items():
            path = self.extracted / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        for folder in ('illustration', 'illustrationBlur', 'illustrationLowRes', 'music'):
            path = self.extracted / folder
            path.mkdir()
            (path / ('Song.A.ogg' if folder == 'music' else 'Song.A.png')).write_bytes(
                self.audio if folder == 'music' else b'png')

    def release(self):
        return organize_release(self.extracted, self.root / 'release', '3.20.0')

    def test_complete_release_reuses_local_bundle_directory(self):
        first = self.release()
        self.assertEqual(validate_release(first), {'songCount': 1, 'musicCount': 1, 'missingResources': []})
        second = self.release()
        self.assertEqual(first['current']['resourceVersion'], 'bundle')
        self.assertEqual(second['current']['resourceVersion'], 'bundle')
        self.assertEqual(first['version_dir'], second['version_dir'])
        self.assertEqual(len(second['current']['manifestSha256']), 64)

    def test_empty_music_directory_cannot_publish(self):
        (self.extracted / 'music/Song.A.ogg').unlink()
        with self.assertRaisesRegex(ValueError, 'music/Song.A.ogg'):
            self.release()

    def test_invalid_music_cannot_publish(self):
        (self.extracted / 'music/Song.A.ogg').write_bytes(b'not audio' * 20)
        with self.assertRaisesRegex(ValueError, 'invalid OGG'):
            self.release()

    def test_missing_difficulty_cannot_publish(self):
        (self.extracted / 'chart/Song.A.0/EZ.json').unlink()
        with self.assertRaisesRegex(ValueError, 'EZ.json'):
            self.release()

    def test_numbered_chart_variants_do_not_count_as_missing_defaults(self):
        (self.extracted / 'metadata/difficulty.tsv').write_text('Song.A\t1\t5\t10\n')
        for variant in range(7):
            if variant:
                (self.extracted / f'music/Song.A.{variant}.ogg').write_bytes(self.audio)
            directory = self.extracted / f'chart/Song.A.{variant}'
            directory.mkdir(exist_ok=True)
            for level in ('EZ', 'HD', 'IN'):
                (directory / f'{level}.json').write_text(json.dumps({'judgeLineList': []}))
        release = self.release()
        self.assertEqual(validate_release(release)['missingResources'], [])
        manifest = json.loads((Path(release['version_dir']) / 'manifest.json').read_text())
        self.assertEqual(sum(asset['path'].startswith('charts/') for asset in manifest['assets']), 21)

    def test_variant_can_use_shared_song_music(self):
        alternate = self.extracted / 'chart/Song.A.1'
        alternate.mkdir()
        (alternate / 'EZ.json').write_text(json.dumps({'judgeLineList': []}))
        self.assertEqual(validate_release(self.release())['missingResources'], [])

    def test_variant_cannot_hide_a_missing_default_difficulty(self):
        (self.extracted / 'metadata/difficulty.tsv').write_text('Song.A\t1\t5\n')
        alternate = self.extracted / 'chart/Song.A.1'
        alternate.mkdir()
        (alternate / 'HD.json').write_text(json.dumps({'judgeLineList': []}))
        with self.assertRaisesRegex(ValueError, 'HD.json'):
            self.release()

    def test_multiple_variants_without_a_default_remain_ambiguous(self):
        default = self.extracted / 'chart/Song.A.0'
        default.rename(self.extracted / 'chart/Song.A.1')
        alternate = self.extracted / 'chart/Song.A.2'
        alternate.mkdir()
        (alternate / 'EZ.json').write_text(json.dumps({'judgeLineList': []}))
        with self.assertRaisesRegex(ValueError, 'EZ.json'):
            self.release()

    def test_header_only_music_cannot_publish(self):
        (self.extracted / 'music/Song.A.ogg').write_bytes(b'OggS' + bytes(24) + b'\x01vorbis' + bytes(40))
        with self.assertRaisesRegex(ValueError, 'invalid OGG'):
            self.release()






    def test_same_size_corruption_rejected(self):
        release = self.release()
        (Path(release['version_dir']) / 'illustrations/Song.A.png').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            validate_release(release)


class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = Path(__file__).resolve().parents[1] / 'bundled/phiTool/script-py'
        sys.path.insert(0, str(directory))
        try:
            spec = importlib.util.spec_from_file_location('publisher_resource_test', directory / 'resource.py')
            cls.resource = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.resource)
        finally:
            sys.path.remove(str(directory))

    def test_music_worker_failure_reaches_caller(self):
        with self.assertRaisesRegex(RuntimeError, 'broken.ogg'):
            with self.resource.CheckedThreadPoolExecutor(2) as pool:
                pool.submit(Mock(side_effect=ValueError('decode failed')), 'broken.ogg')

    def test_new_illustrations_choose_plain_then_highest_unlocked_difficulty(self):
        table = [
            ['Song.A.0/Illustration_HD.png', 'hd'], ['Song.A.0/Illustration_AT.png', 'at'],
            ['Song.A.0/Illustration.png.c9Locked', 'locked'],
            ['Song.B.0/Illustration_AT.png', 'b-at'], ['Song.B.0/Illustration.png', 'plain'],
            ['Song.A.0/IllustrationBlur_IN.png', 'blur'], ['Song.A.1/Chart_IN.json', 'variant'],
        ]
        selected = dict(self.resource.apply_illustration_precedence(table))
        self.assertEqual(selected, {'Song.A.0/Illustration.png': 'at', 'Song.B.0/Illustration.png': 'plain',
                                    'Song.A.0/IllustrationBlur.png': 'blur', 'Song.A.1/Chart_IN.json': 'variant'})

    def test_new_bundle_names_fall_back_to_the_hash_entry(self):
        self.assertEqual(self.resource.resolve_bundle_path({'assets/aa/Android/hash.bundle'}, 'group_hash.bundle'),
                         'assets/aa/Android/hash.bundle')

    def test_metadata_loads_packed_and_split_unity_payloads(self):
        import io
        from zipfile import ZipFile
        directory = Path(__file__).resolve().parents[1] / 'bundled/phiTool/script-py'
        sys.path.insert(0, str(directory))
        try:
            spec = importlib.util.spec_from_file_location('metadata_test', directory / 'gameInformation.py')
            metadata = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(metadata)
        finally:
            sys.path.remove(str(directory))
        for names in (['assets/bin/Data/data.unity3d'], ['assets/bin/Data/globalgamemanagers.assets', 'assets/bin/Data/level0']):
            data = io.BytesIO()
            with ZipFile(data, 'w') as archive:
                for name in names:
                    archive.writestr(name, name.encode())
            payloads = []
            environment = Mock(load_file=lambda file, name: payloads.append((name, file.read())))
            with ZipFile(data) as archive:
                metadata.load_game_data(archive, environment)
            self.assertEqual(payloads, [(name, name.encode()) for name in names])

    def test_write_failure_reaches_caller(self):
        with patch('builtins.open', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'chart.json'):
                self.resource.write_resource(('chart.json', b'chart'))

    def test_chart_variants_share_default_music(self):
        obj = Mock()
        entry = Mock()
        entry.get_filtered_objects.side_effect = lambda _: iter([Mock(read=lambda: obj)])
        pool = Mock()
        for variant in (0, 1, 6):
            self.resource.save(f'Random.SobremSilentroom.{variant}/music.wav', entry, pool, Mock(),
                               {'music': 'music'}, dict(avatar=False, chart=False, illustrationBlur=False,
                               illustrationLowRes=False, illustration=False, music=True))
        self.assertEqual([Path(call.args[1]).name for call in pool.submit.call_args_list],
                         ['Random.SobremSilentroom.ogg'])


if __name__ == '__main__':
    unittest.main()
