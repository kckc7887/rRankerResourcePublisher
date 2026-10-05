# phiTool - Phigros 数据管理工具
# Copyright (C) 2026 Chnynnya
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import base64
from concurrent.futures import ThreadPoolExecutor
from configparser import ConfigParser
import gc
from io import BytesIO
import json
import os
import re
import shutil
import sys
import time
from threading import BoundedSemaphore, Lock
from UnityPy import Environment
from UnityPy.classes import AudioClip
from UnityPy.enums import ClassIDType
from zipfile import ZipFile
try:
    from .log import init_console_logger
    from .gameInformation import run as extract_metadata
except ImportError:
    from log import init_console_logger
    from gameInformation import run as extract_metadata
import logging



class ByteReader:
    def __init__(self, data):
        self.data = data
        self.position = 0

    def readInt(self):
        self.position += 4
        return self.data[self.position - 4] ^ self.data[self.position - 3] << 8 ^ self.data[self.position - 2] << 16


def write_resource(item):
    path, resource = item
    try:
        with open(path, "wb") as sink:
            if isinstance(resource, BytesIO):
                with resource:
                    sink.write(resource.getbuffer())
            else:
                sink.write(resource)
    except Exception as error:
        raise RuntimeError(f"资源写入失败：{path}") from error


class CheckedThreadPoolExecutor(ThreadPoolExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = BoundedSemaphore(self._max_workers * 2)
        self._task_lock = Lock()
        self._pending = set()
        self._failure = None

    def _raise_failure(self):
        if self._failure is not None:
            path, error = self._failure
            raise RuntimeError(f"资源提取失败：{path}") from error

    def submit(self, fn, *args, **kwargs):
        self._slots.acquire()
        try:
            with self._task_lock:
                self._raise_failure()
                future = super().submit(fn, *args, **kwargs)
                self._pending.add(future)
        except BaseException:
            self._slots.release()
            raise

        def completed(task):
            failed = not task.cancelled() and task.exception() is not None
            with self._task_lock:
                self._pending.discard(task)
                if failed and self._failure is None:
                    self._failure = (args[0] if args else fn.__name__, task.exception())
                cancel = list(self._pending) if failed else []
            self._slots.release()
            for queued in cancel:
                queued.cancel()

        future.add_done_callback(completed)
        return future

    def __exit__(self, exc_type, exc_value, traceback):
        self.shutdown(wait=True, cancel_futures=exc_type is not None or self._failure is not None)
        if exc_type is None:
            self._raise_failure()
        return False


class _BundleWriter:
    """Save within the parser worker so its Unity objects stay worker-local."""

    @staticmethod
    def submit(function, *args):
        return function(*args)


def save_image(path, image):
    bytesIO = BytesIO()
    image.save(bytesIO, "png")
    write_resource((path, bytesIO))


def save_music(path, music: AudioClip):
    # 惰性导入：publisher 通过 run() 直接调用本模块时不会执行 __main__ 分支，
    # 必须在保存音乐时自行加载 fsb5。
    from fsb5 import FSB5
    fsb = FSB5(music.m_AudioData)
    rebuilt_sample = bytes(fsb.rebuild_sample(fsb.samples[0]))
    if not rebuilt_sample.startswith(b"OggS"):
        raise ValueError(f"音乐不是有效 OGG：{path}")
    write_resource((path, rebuilt_sample))


classes = ClassIDType.TextAsset, ClassIDType.Sprite, ClassIDType.AudioClip


ILLUSTRATION_FAMILIES = ("Illustration", "IllustrationBlur", "IllustrationLowRes")
ILLUSTRATION_DIFFICULTY_ORDER = ("AT", "IN", "HD", "EZ")


def illustration_candidate(key):
    """返回 (歌曲目录, 曲绘族, 扩展名, 优先级)：锁定版优先级为 None，非曲绘地址整体返回 None。"""
    song_dir, separator, asset = key.partition("/")
    if not separator or not song_dir.endswith(".0"):
        return None
    for family in ILLUSTRATION_FAMILIES:
        if asset.startswith("%s." % family) and asset.endswith(".c9Locked"):
            return song_dir, family, None, None
    stem, dot, extension = asset.rpartition(".")
    if not dot or not extension:
        return None
    for family in ILLUSTRATION_FAMILIES:
        if stem == family:
            return song_dir, family, extension, 0
        if stem.startswith("%s_" % family):
            difficulty = stem[len(family) + 1:]
            if difficulty in ILLUSTRATION_DIFFICULTY_ORDER:
                return song_dir, family, extension, ILLUSTRATION_DIFFICULTY_ORDER.index(difficulty) + 1
    return None


def apply_illustration_precedence(table):
    """每首歌每个曲绘族只保留优先级最高的一条（锁定版丢弃），并把胜出者改写成普通曲绘地址。"""
    winners = {}
    for key, _value in table:
        candidate = illustration_candidate(key)
        if candidate is None:
            continue
        song_dir, family, extension, rank = candidate
        if rank is None:
            continue
        previous = winners.get((song_dir, family))
        if previous is None or rank < previous[0]:
            winners[(song_dir, family)] = (rank, key, extension)

    selected = []
    for key, value in table:
        candidate = illustration_candidate(key)
        if candidate is None:
            selected.append([key, value])
            continue
        song_dir, family, _extension, rank = candidate
        if rank is None:
            continue
        winner = winners[(song_dir, family)]
        if winner[1] != key:
            continue
        selected.append(["%s/%s.%s" % (song_dir, family, winner[2]), value])
    return selected


def save(key, entry, pool, logger, output_dirs, config):
    obj = entry.get_filtered_objects(classes)
    obj = next(obj).read()
    if config["avatar"] and key[:7] == "avatar.":
        key = key[7:]
        pool.submit(save_image, os.path.join(output_dirs["avatar"], "%s.png" % key), obj.image)
    elif config["chart"] and key[-14:-7] == "/Chart_" and key[-5:] == ".json":
        logger.info(key)
        p = os.path.join(output_dirs["chart"], key[:-14])
        os.makedirs(p, exist_ok=True)
        pool.submit(write_resource, (os.path.join(output_dirs["chart"], "%s/%s.json" % (key[:-14], key[-7:-5])), obj.script))
    elif config["illustrationBlur"] and key[-23:-3] == ".0/IllustrationBlur.":
        key = key[:-23]
        pool.submit(save_image, os.path.join(output_dirs["illustrationBlur"], "%s.png" % key), obj.image)
    elif config["illustrationLowRes"] and key[-25:-3] == ".0/IllustrationLowRes.":
        key = key[:-25]
        pool.submit(save_image, os.path.join(output_dirs["illustrationLowRes"], "%s.png" % key), obj.image)
    elif config["illustration"] and key[-19:-3] == ".0/Illustration.":
        key = key[:-19]
        pool.submit(save_image, os.path.join(output_dirs["illustration"], "%s.png" % key), obj.image)
    elif config["music"] and re.search(r"\.\d+/music\.wav$", key):
        key = key[:-10]
        if key.endswith('.0'):
            key = key[:-2]
        pool.submit(save_music, os.path.join(output_dirs["music"], "%s.ogg" % key), obj)
        # save_music(f"music/{key}.wav", obj)


def resolve_bundle_path(available, entry):
    """定位 APK 内的 bundle 文件。

    catalog 里的名字在 4.0.0 起带上了 <group>_ 前缀，而 APK 内只有 <hash>.bundle。
    """
    name = str(entry)
    path = "assets/aa/Android/%s" % name
    if path not in available:
        path = "assets/aa/Android/%s" % name.rsplit("_", 1)[-1]
    return path

def _extract_bundle(item, path, logger, output_dirs, config):
    key, entry = item
    # Neither ZipFile nor lazy Unity objects cross worker boundaries. Keeping the
    # environment alive through saving also bounds decoded image/audio memory.
    with ZipFile(path) as apk:
        payload = apk.read(resolve_bundle_path(set(apk.namelist()), entry))
    with BytesIO(payload) as bundle:
        env = Environment()
        env.load_file(bundle, name=key)
        writer = _BundleWriter()
        for i_key, i_entry in sorted(env.files.items()):
            save(i_key, i_entry, writer, logger, output_dirs, config)


def run(path, config, logger, metadata_dir="info", output_dirs=None, workers=4):
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ValueError("资源处理线程数必须为 1-16 的整数")
    if output_dirs is None:
        output_dirs = {
            "avatar": "avatar",
            "chart": "chart",
            "illustrationBlur": "illustrationBlur",
            "illustrationLowRes": "illustrationLowRes",
            "illustration": "illustration",
            "music": "music"
        }

    for key, dir_path in output_dirs.items():
        if key in config and config[key]:
            os.makedirs(dir_path, exist_ok=True)

    os.makedirs(metadata_dir, exist_ok=True)

    tmp_tsv_path = os.path.join(metadata_dir, "tmp.tsv")
    difficulty_tsv_path = os.path.join(metadata_dir, "difficulty.tsv")

    if not os.path.exists(tmp_tsv_path) or not os.path.exists(difficulty_tsv_path):
        logger.info("元数据文件不存在，先提取元数据...")
        extract_metadata(path, logger, metadata_dir)
        logger.info("元数据提取完成")

    with ZipFile(path) as apk:
        with apk.open("assets/aa/catalog.json") as f:
            data = json.load(f)

    key = base64.b64decode(data["m_KeyDataString"])
    bucket = base64.b64decode(data["m_BucketDataString"])
    entry = base64.b64decode(data["m_EntryDataString"])

    table = []
    reader = ByteReader(bucket)
    for x in range(reader.readInt()):
        key_position = reader.readInt()
        key_type = key[key_position]
        key_position += 1
        if key_type == 0:
            length = key[key_position]
            key_position += 4
            key_value = key[key_position:key_position + length].decode()
        elif key_type == 1:
            length = key[key_position]
            key_position += 4
            key_value = key[key_position:key_position + length].decode("utf16")
        elif key_type == 4:
            key_value = key[key_position]
        else:
            raise BaseException(key_position, key_type)
        entry_value = None
        for i in range(reader.readInt()):
            entry_position = reader.readInt()
            entry_value = entry[4 + 28 * entry_position:4 + 28 * entry_position + 28]
            entry_value = entry_value[8] ^ entry_value[9] << 8
        table.append([key_value, entry_value])
    for i in range(len(table)):
        if table[i][1] != 65535:
            table[i][1] = table[table[i][1]][0]
    for i in range(len(table) - 1, -1, -1):
        if type(table[i][0]) == int or table[i][0][:15] == "Assets/Tracks/#" or table[i][0][:14] != "Assets/Tracks/" and \
                table[i][0][:7] != "avatar.":
            del table[i]
        elif table[i][0][:14] == "Assets/Tracks/":
            table[i][0] = table[i][0][14:]
    table = apply_illustration_precedence(table)
    for key, value in table:
        logger.info('{key}, {value}'.format(key=key, value=value))

    if config["avatar"]:
        avatar = {}
        with open(os.path.join(metadata_dir, "tmp.tsv"), encoding="utf8") as f:
            line = f.readline()[:-1]
            while line:
                l = line.split("\t")
                avatar[l[1]] = l[0]
                line = f.readline()[:-1]

    ti = time.time()
    update = config["UPDATE"]
    if update["main_story"] != 0 or update["other_song"] != 0 or update["side_story"] != 0:
        l = []
        with open(os.path.join(metadata_dir, "difficulty.tsv"), encoding="utf8") as f:
            line = f.readline()
            while line:
                l.append(line.split("\t", 2)[0])
                line = f.readline()
        index1 = l.index("Doppelganger.LeaF")
        index2 = l.index("Poseidon.1112vsStar")
        del l[index2:len(l) - update["side_story"]]
        del l[index1:index2 - update["other_song"]]
        del l[:index1 - update["main_story"]]
        logger.info(str(l))
        table = [(key, entry) for key, entry in table
                 if key.startswith("avatar.") or any(key.startswith("%s.0/" % song_id) for song_id in l)]
    with CheckedThreadPoolExecutor(max_workers=workers, thread_name_prefix="phigros-extract") as pool:
        for item in sorted(set(map(tuple, table))):
            pool.submit(_extract_bundle, item, path, logger, output_dirs, config)
    logger.info("%f秒" % round(time.time() - ti, 4))


if __name__ == "__main__":
    if len(sys.argv) == 1 and os.path.isdir("/data/"):
        import subprocess
        r = subprocess.run("pm path com.PigeonGames.Phigros",stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,shell=True)
        file_path = r.stdout[8:-1].decode()
    else:
        file_path = sys.argv[1]
    c = ConfigParser()
    c.read("config.ini", "utf8")
    types = c["TYPES"]
    config = {
        "avatar": types.getboolean("avatar"),
        "chart": types.getboolean("Chart"),
        "illustrationBlur": types.getboolean("IllustrationBlur"),
        "illustrationLowRes": types.getboolean("IllustrationLowRes"),
        "illustration": types.getboolean("Illustration"),
        "music": types.getboolean("music"),
        "UPDATE": {
            "main_story": c["UPDATE"].getint("main_story"),
            "side_story": c["UPDATE"].getint("side_story"),
            "other_song": c["UPDATE"].getint("other_song")
        }
    }
    if config["music"]:
        from fsb5 import FSB5
        from fsb5 import vorbis
    type_list = ("avatar", "chart", "illustrationBlur", "illustrationLowRes", "illustration", "music")
    for directory in type_list:
        if not config[directory]:
            continue
        if not os.path.isdir(directory):
            os.mkdir(directory)
        if os.path.isdir("/system/") and not os.getcwd().startswith("/data/"):
            with open(directory + "/.nomedia", "wb"):
                pass
    run(file_path, init_console_logger())
