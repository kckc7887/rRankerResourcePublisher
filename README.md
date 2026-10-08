# rRankerResourcePublisher

统一发布 rRanker 使用的 Phigros、Rizline、Kyou 资源、正式 Release APK，以及 maimai DXTag。Phigros、Rizline、Kyou 的解析模块保留各自数据口径，对象存储、差量计划、分片执行和入口切换共用 `publisher`。

## 发布入口

| 工作流 | 北京时间 | Environment |
| --- | --- | --- |
| `rizline.yml` | 每天 08:00 | `S3_BUCKET_RIZLINE` |
| `kyou.yml` | 每天 08:00 | `S3_BUCKET_PHIGROS` |
| `phigros.yml` | 每天 12:00 | `S3_BUCKET_PHIGROS` |
| `dxtag.yml` | 每天 12:00 | `S3_BUCKET_MAIMAI` |
| `apk.yml` | rRanker 正式 Release published | `S3_BUCKET_RRANKER` |

资源工作流只有仓库变量 `RESOURCE_SCHEDULES_ENABLED=true` 时执行定时发布。手动运行默认仅生成计划；`migrate` 直接采用当前桶内字节，跳过上游解析。实际发布仅允许 `master`。push/PR 的 `ci.yml` 只运行解析和发布行为测试、工作流检查，不使用存储凭据。

各 Environment 提供变量 `S3_BUCKET_NAME` 和 Secrets `S3_ACCESS_KEY_ID`、`S3_SECRET_ACCESS_KEY`；仓库 Secret 为 `S3_ENDPOINT`。Phigros 与 Kyou 使用同一桶、独立资源组。`S3_BUCKET_MAIMAI` 的桶名是 `rranker-maimai-data`。所有删除均依据具体哈希对象，不执行桶根同步。

## 对象与合同

每组的 `<组名>/latest.json` 使用 schemaVersion 2，指向 `manifests/<sha256>.json` 并携带 `manifestSha256`、`resourceVersion`、`publishedAt`。清单记录 `previousManifest`、发布时间和完整资源集合。

| 资源组 | 分类目录 |
| --- | --- |
| Phigros | `avatars`、`charts`、`illustrations`、`illustrations-blur`、`illustrations-lowres`、`music`、`metadata` |
| Rizline | `covers`、`audio`、`charts`、`metadata` |
| Kyou | `data` |

`DXTag/{谱面文件ID}.json` 在 maimai 桶内按固定 ID 存放，不进入上述清单和清理。标准谱的 ID 是歌曲 ID，DX 谱的 ID 是歌曲 ID 加 10000。宴谱不生成对象。每份文件是难度 ID 与五维数组的列表；难度 ID 为 `0` 到 `4`，五维顺序为键盘、星星、技巧、体力、爆发。已有单曲对象不重新计算、不覆盖、不删除。手动运行默认只列出缺失 ID。

`DXTag/all.json` 是独立全曲库版，JSON 对象的键为谱面文件 ID 字符串，值为对应单曲文件的难度与五维数组列表，包含当前 LXNS 曲库的全部标准谱和 DX 谱。每天定时发布或手动开启 `execute` 时，先补齐单曲对象，再读取当前曲库对应的全部单曲对象并更新全曲库版；没有新增谱面时也会更新。单曲补齐或汇总读取失败时保留上一次全曲库文件，任务报错。全曲库地址不使用永久缓存，也不进入其他资源组的清理。

对象名为 `<sha256>.<扩展名>`。Phigros 清单 `assets` 中的 `path` 是歌曲、难度、变体的逻辑地址，`objectKey` 是实际桶路径，同时记录 `size`、`sha256`、`contentType`。指针的 `catalog`、`noteCounts` 必须与对应清单项一致。Rizline 清单使用 `files` 与 `catalogPath`，曲库保持 schemaVersion 1，内部引用直接使用固定对象路径。Kyou 清单保留抓取统计，`files` 增加 `name` 到 `path/size/sha256/contentType` 的映射。

Phigros 的物量表每行按歌曲排列 EZ、HD、IN、可选 AT，各难度为 `[Tap,Hold,Drag,Flick]`；谱面根节点 `blockAreaList` 非空时，追加其数组长度作为第五项 BLOCK。缺失、null 或空数组不追加第五项，异常非数组值使统计失败。BLOCK 独立显示，总物量仅累加四种音符。

内容身份不包含发布或抓取时间。无变化时不上传、不复制、不回读大资源；单个文件改变只上传该对象。Phigros、Rizline 的差量按字节量均衡分到最多 8 个 `upload-shard.yml` 子工作流，子工作流只下载自己的增量产物。Kyou 在单独抓取作业内完成发布。资源组入口串行，各组可并行，子工作流不持有父工作流锁。

Phigros 媒体项另存 `contentSha256`：PNG 校验像素、尺寸和图像元数据；Vorbis 校验识别头、配置头、音频包、用户标签及采样位置，仅忽略工具的 vendor 标识。只有内容一致时才复用已有对象及其原始 SHA-256，避免不同平台的 PNG 压缩和 Vorbis 库标识引起整批重传。首次建立指纹时完整读取并核验已有媒体，之后直接使用清单中的指纹；缺失或损坏的基线不回退上传。音乐提取保留共用曲目，谱面变体通过逻辑路径关联。

全部分片完成内容校验后才能写清单、条件更新入口并回读。失败、取消、缺失回执、源 ETag 变化或入口条件冲突均不切换入口。当前及上一版始终保留，其他清单从退役开始至少保留 7 天；清理仅删除未被保留清单引用且已超过保留期的哈希对象。

## 首次迁移

先完成本地和两仓 CI 验证，再手动执行 `migrate`。计划逐对象记录复制源 ETag、目标键、大小和 SHA-256，汇总原址复用、桶内复制、新上传、删除数量与字节数。现有目标对象校验后复用，其余已有媒体执行 `CopyObject`；Rizline 只重新写入含新路径的小型曲库。缺失或不可靠的基线直接报错，不回退全量上传。结构迁移和上游重新解析分别运行。

迁移不修改旧 `current.json`、`kyou/latest/` 与旧资源目录。`phigros/chapters.csv`、`chart-preview/`、`fonts/`，以及 rranker 桶的 `assets/`、`release/0.3.0/` 均不在资源发布或清理范围内。旧入口在包含新客户端的正式 Release 验收后，依据迁移计划中的具体源对象单独退役。

## APK

本体的 `RESOURCE_PUBLISHER_TOKEN` 仅授予本仓库 Actions 读写权限，用于发送 Release ID 和查询运行结果。发布器自行读取 GitHub 正式 Release，拒绝草稿、预发布和迟到的旧版本。四个附件必须全部完成大小、摘要、ZIP、ABI、包名、版本与签名校验，再写入：

```text
release/rRanker-arm64.apk
release/rRanker-armeabi.apk
release/rRanker-x86.apk
release/rRanker-x86_64.apk
```

固定 APK 地址不使用永久缓存。四个地址分别提交，失败报告明确列出已完成对象，同一 Release 可重试。`bootstrap` 仅从现有 0.3.0 的四个对象桶内复制，保留来源目录。Android 签名证书与本体当前生产构建校验合同一致。

## 本地命令

使用 Python 3.13，安装 `requirements.txt`。Phigros 需要 FFmpeg 和系统 libogg/libvorbis；Rizline 还需要 vgmstream-cli；Kyou 使用 Playwright Chromium，Linux 无桌面运行时使用 `xvfb-run`。APK 校验使用 Android SDK Build Tools。

```sh
python -m unittest discover -s tests -v
python -m publisher build phigros --output work/build
python -m publisher prepare phigros --input work/build --output work/publication
python -m publisher migrate phigros --output work/migration
python -m publisher gc phigros
python -m publisher dxtag
```

`prepare`、`migrate` 和默认 `gc` 只读远端。`shard` 执行单片对象写入，`finalize` 检查全部回执并提交入口。`publish-plan` 顺序执行计划与提交，用于 Kyou；`gc --execute` 删除已核实的退役对象。生产流水线对 Phigros 和 Rizline 使用子工作流。

`rizline_publisher/overrides.json` 保留人工修订。Rizline 仅在一首歌全部谱面均为 EZ/HD/IN、定数 99 且在 pigeonCN 替换后的官方索引中全部缺失时跳过预告曲，并输出 `skippedPreviewSongs`；普通或部分缺失仍失败。Phigros 支持压缩 Unity 数据布局、资源包名称解析、未锁定曲绘优先级、默认谱及编号变体、独立或共用音乐和物量表。Kyou 保留隔离浏览器上下文、批量标签请求和完整性检查，定时任务不自动退化为数千次逐谱请求。

## 许可与来源

Phigros 内置工具源码保留 Copyright (C) 2026 Chnynnya 和 GPL-3.0-or-later 声明；[上游来源快照](https://github.com/kckc7887/Phigros_Resource/tree/38e8cb8e9f9f494ea31e08f98ca48169d89238c9)，完整许可证位于 [LICENSES/phiTool-GPL-3.0.txt](LICENSES/phiTool-GPL-3.0.txt)。修改后的解析源码随本仓库提供。

Rizline 的 CRI UTF、AFS2、HCA 元数据核验参考 [vgmstream 固定提交](https://github.com/vgmstream/vgmstream/tree/e6afeaacf433bfafd38d873f80c94517e09d5b96) 中 `src/util/cri_utf.c`、`src/meta/awb.c`、`src/coding/libs/clhca.c`。完整 ISC 式许可与 Portions 归属保存在 [LICENSES/vgmstream-COPYING.txt](LICENSES/vgmstream-COPYING.txt)。保留 HCA 文件头的来源归属：nyaga 的反编译与 C++ 解码器、kode54 的 C 移植、bnnm 的整理与 HCA v3 分析、Thealexbarney 的 VGAudio 参考、Youjose 的 Ambisonics 信息。Python 实现只读元数据，不内嵌解码器；解码调用外部 vgmstream-cli，Actions 固定使用 r2117 并核验下载摘要，编码调用外部 FFmpeg。

统计补充只读取 [rizline-tool 固定提交](https://github.com/limmy114/rizline-tool/blob/a7e1ae23aaae215c36710899af363bc71ae32634/index.html) 的 `songAllData` 数据字面量，不执行或复制其前端算法。该数据来源未提供独立代码许可证。依赖通过 pip 安装并保留分发包自带许可证；UnityPy 为 MIT，Pillow 为 MIT-CMU，boto3、requests、Playwright 为 Apache-2.0，fsb5 为 MIT。仓库不捆绑上述依赖和 FFmpeg 二进制。

## 致谢

- [Chnynnya/phiTool](https://github.com/Chnynnya/phiTool)
- [kckc7887/Phigros_Resource](https://github.com/kckc7887/Phigros_Resource)
- [vgmstream/vgmstream](https://github.com/vgmstream/vgmstream)
- [limmy114/rizline-tool](https://github.com/limmy114/rizline-tool)
- [CHCAT1320/rizline-assets-get](https://github.com/CHCAT1320/rizline-assets-get)
- [kckc7887/kyou-crawler](https://github.com/kckc7887/kyou-crawler)
- [kckc7887/DXTag](https://github.com/kckc7887/DXTag)
