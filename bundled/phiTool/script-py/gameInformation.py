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

import json
import os
import sys
from UnityPy import Environment
import zipfile
from io import BytesIO
from log import init_console_logger
import logging

DEBUG = False

DATA_DIR = "assets/bin/Data/"
# Phigros 4.0.1 起 Unity 数据整体打进 data.unity3d，不再有 globalgamemanagers.assets / level0。
PACKED_DATA_FILE = DATA_DIR + "data.unity3d"
SPLIT_DATA_FILES = (DATA_DIR + "globalgamemanagers.assets", DATA_DIR + "level0")


def data_file_entries(names):
    """返回需要加载的 Unity 数据文件，兼容 4.0.0 及更早的 APK 布局。"""
    available = set(names)
    if PACKED_DATA_FILE in available:
        return [PACKED_DATA_FILE]
    return [name for name in SPLIT_DATA_FILES if name in available]


def load_game_data(apk, env):
    entries = data_file_entries(apk.namelist())
    if not entries:
        raise FileNotFoundError(
            "APK 内未找到 Unity 数据文件：%s 缺失，%s 也缺失"
            % (PACKED_DATA_FILE, "、".join(SPLIT_DATA_FILES))
        )
    # UnityPy 解析外部引用时以 env.path 为基准拼相对路径；打包布局里存在指向
    # Unity 内置库（Library/unity default resources）的引用，path 为 None 时
    # os.path.join 会抛 TypeError 并中断整个遍历。指向脚本目录让它正常判为找不到。
    env.path = os.path.dirname(os.path.abspath(__file__))
    for name in entries:
        with apk.open(name) as f:
            env.load_file(BytesIO(f.read()), name=name)


def script_name(env, obj):
    """返回 MonoBehaviour 的脚本名；脚本不在 APK 内（Unity 内置库）时返回 None。"""
    pointer = obj.read().m_Script
    external_name = pointer.external_name
    # 外部脚本不在 APK 内时 UnityPy 会打印依赖缺失并去磁盘找；这里直接跳过。
    if external_name and env.get_cab(external_name) is None:
        return None
    script = pointer.get_obj()
    if script is None:
        return None
    return script.read().name


def run(path, logger, output_dir="info"):
    Tips = None
    GameInformation = None
    Collections = None
    os.makedirs(output_dir, exist_ok=True)
    typetree_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "typetree.json")
    with open(typetree_path) as f:
        typetree = json.load(f)
    env = Environment()
    with zipfile.ZipFile(path) as apk:
        load_game_data(apk, env)
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        name = script_name(env, obj)
        if name == "GameInformation":
            GameInformation = obj.read_typetree(typetree["GameInformation"])
        elif name == "GetCollectionControl":
            Collections = obj.read_typetree(typetree["GetCollectionControl"], True)
        elif name == "TipsProvider":
            Tips = obj.read_typetree(typetree["TipsProvider"], True)

    difficulty = []
    table = []
    for key, songs in GameInformation["song"].items():
        if key == "otherSongs":
            continue
        for song in songs:
            if len(song["difficulty"]) == 5:
                song["difficulty"].pop()
            if song["difficulty"][-1] == 0.0:
                song["difficulty"].pop()
                song["charter"].pop()
            for i in range(len(song["difficulty"])):
                song["difficulty"][i] = str(round(song["difficulty"][i], 1))
            song["songsId"] = song["songsId"][:-2]
            difficulty.append([song["songsId"]]+song["difficulty"])
            table.append((song["songsId"], song["songsName"], song["composer"], song["illustrator"], *song["charter"]))

    logger.info(difficulty)
    logger.info(table)

    with open(os.path.join(output_dir, "difficulty.tsv"), "w", encoding="utf8") as f:
        for item in difficulty:
            f.write("\t".join(map(str, item)))
            f.write("\n")

    with open(os.path.join(output_dir, "info.tsv"), "w", encoding="utf8") as f:
        for item in table:
            f.write("\t".join(item))
            f.write("\n")

    single = []
    illustration = []
    for key in GameInformation["keyStore"]:
        if key["kindOfKey"] == 0:
            single.append(key["keyName"])
        elif key["kindOfKey"] == 2 and key["keyName"] != "Introduction" and key["keyName"] not in single:
            illustration.append(key["keyName"])

    with open(os.path.join(output_dir, "single.txt"), "w", encoding="utf8") as f:
        for item in single:
            f.write("%s\n" % item)

    with open(os.path.join(output_dir, "illustration.txt"), "w", encoding="utf8") as f:
        for item in illustration:
            f.write("%s\n" % item)
    logger.info(single)
    logger.info(illustration)

    D = {}
    for item in Collections.collectionItems:
        if item.key in D:
            D[item.key][1] = item.subIndex
        else:
            D[item.key] = [item.multiLanguageTitle.chinese, item.subIndex]

    with open(os.path.join(output_dir, "collection.tsv"), "w", encoding="utf8") as f:
        for key, value in D.items():
            f.write("%s\t%s\t%s\n" % (key, value[0], value[1]))

    with open(os.path.join(output_dir, "avatar.txt"), "w", encoding="utf8") as avatar:
        with open(os.path.join(output_dir, "tmp.tsv"), "w", encoding="utf8") as tmp:
            for item in Collections.avatars:
                avatar.write(item.name)
                avatar.write("\n")
                tmp.write("%s\t%s\n" % (item.name, item.addressableKey[7:]))

    with open(os.path.join(output_dir, "tips.txt"), "w", encoding="utf8") as f:
        for tip in Tips.tips[0].tips:
            f.write(tip)
            f.write("\n")


if __name__ == "__main__":
    if len(sys.argv) == 1 and os.path.isdir("/data/"):
        import subprocess
        r = subprocess.run("pm path com.PigeonGames.Phigros",stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,shell=True)
        file_path = r.stdout[8:-1].decode()
    else:
        file_path = sys.argv[1]
    if not os.path.isdir("info"):
        os.mkdir("info")
    run(file_path, init_console_logger())
