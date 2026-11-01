# RMTC 2026 图灵对决赛 · 参赛材料仓库

ROS 2 Humble 参赛节点 `rmtc_bot` 的代码、镜像与文档，按递交轮次分目录存放，
每轮自带可独立还原的代码、镜像分卷、文档与校验和。

随机事件只通过官方自然语言公告交给 LLM 理解，模型输出仅做几何合法性校验。

## 轮次索引

| 轮次 | 目录 | 镜像 | 代码 | 内容 |
| --- | --- | --- | --- | --- |
| 2026-10-31 递交（首轮） | [`20261031/`](20261031/) | `rmtc-team:20261029`（`068321cfe973`） | `bot_node.py` `c1a25510…`，1607 行 | 正式递交包：提交说明、检录录屏、3 卷镜像、技术报告草稿、代码逐块说明、现场操作备忘 |
| 2026-11-01 更新 | [`20261101/`](20261101/) | `rmtc-team:20261101`（`21930139bd78`） | `bot_node.py` `a436aeda…`，1749 行 | 现场问题修补 11 条 + 1 处崩溃修复，含改动说明与 15:24 场赛况还原；**未实跑验证** |

两轮互不覆盖：`20261031/` 保持首轮原样，`20261101/` 是其后所有改动的落点。

## 仓库结构

```
stars-in-the-sky/
├── README.md                       # 本文件
├── .gitignore
├── 20261031/                       # 首轮递交快照（镜像 tag 20261029）
│   ├── README.md                   # 该轮说明：构建、运行、决策类型
│   ├── Dockerfile / .dockerignore
│   ├── run_contestant.sh           # 启动入口（席位/令牌/网关全部来自环境变量）
│   ├── decision_config.json        # 现场唯一允许改的参数文件
│   ├── fastdds_udp_only.xml
│   ├── src/rmtc_bot/               # ROS 2 包
│   ├── docs/                       # 技术报告草稿、代码逐块说明、检录清单、现场备忘、物料清单
│   ├── submission/                 # 提交说明、两个 sha256、检录录屏
│   └── archive/rmtc-team-image/    # 镜像 3 卷
└── 20261101/                       # 现场修补后快照（镜像 tag 20261101）
    ├── README.md                   # 该轮标识、改动摘要、验证状态
    ├── Dockerfile / .dockerignore / run_contestant.sh / decision_config.json / fastdds_udp_only.xml
    ├── src/rmtc_bot/               # 今日代码
    ├── docs/                       # 今日改动说明、15:24 场赛况报告、技术报告语雀链接与草稿
    ├── submission/                 # 提交说明、两个 sha256
    └── archive/rmtc-team-image/    # 镜像 3 卷
```

## 镜像还原

GitHub 单文件上限 100 MiB，每轮镜像按 90 MiB 分 3 卷存放：

```bash
cd 20261101/archive/rmtc-team-image
cat rmtc-team-image.tar.gz.part* > rmtc-team-image.tar.gz
sha256sum rmtc-team-image.tar.gz          # 见下表
gunzip -c rmtc-team-image.tar.gz | docker load
docker images rmtc-team --format '{{.Repository}}:{{.Tag}} {{.ID}}'
```

`20261031/` 同上，把路径换掉即可。两轮分卷合计约 500 MiB，克隆较慢属正常。

## 校验和一览

| 轮次 | 镜像包 `rmtc-team-image.tar.gz` | 源码包 `rmtc-team-code.tar.gz` |
| --- | --- | --- |
| 20261031 | `ad3dc3b87b03e97bddce8cade36f90c5dd7e73e888c521025d5490a787e2aaab`（262,517,638 字节） | `d6b048d87efb354f0edc7dedaa5d6d159f40913ba497c17bdee0efc1a921f400` |
| 20261101 | `a57b08909696bb8c53ef284327eedfe92e8f52700855c672eec244f4298ddb73`（262,526,073 字节） | `9494a738d6e1a83eec61f71280964979610b322a8541e33a0e945d7a87a6cdec` |

每轮目录下的 `submission/*.sha256` 为权威值；两个 `.tar.gz` 原件体积大，未纳入 git。

## 构建与运行

```bash
cd 20261101                      # 或 20261031
docker build -t rmtc-team:20261101 .

docker run --rm --network host \
  -e RMTC_SEAT=A \
  -e RMTC_TEAM_TOKEN="<本场席位令牌>" \
  -e RMTC_AI_URL="<本场统一 LLM 网关完整 completions 地址>" \
  -e ROS_DOMAIN_ID=27 \
  -e ROS_LOCALHOST_ONLY=0 \
  -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  rmtc-team:20261101
```

- 席位、令牌、网关、ROS 域全部由环境变量注入，代码与镜像内不写死任何值。
- `20261101` 起支持 `-e RMTC_AI_TIMEOUT=8`（秒，默认 8）。
- 缺少 `RMTC_TEAM_TOKEN` 时程序以退出码 2 终止，不会空跑。
- `decision_config.json` 每 20 Tick 重读，暂停期间切换 `collect` / `survive` 后恢复即生效。

## 文档索引

| 文档 | 位置 | 说明 |
| --- | --- | --- |
| 今日改动说明 | [`20261101/docs/今日改动说明_20261101.md`](20261101/docs/今日改动说明_20261101.md) | 11 条改动 + 1 处崩溃修复：前因、改法、验证口径 |
| 15:24 场赛况报告 | [`20261101/docs/比赛报告_20261101_1524.md`](20261101/docs/比赛报告_20261101_1524.md) | AI 全 401、6 次阵亡的过程还原与下一步动作 |
| 技术报告（语雀） | [`20261101/docs/技术报告_语雀.md`](20261101/docs/技术报告_语雀.md) | 正式版在语雀（当前私密，直连 401）；仓库内附 10-30 本地草稿，§6/§8 未同步 |
| 技术报告本地草稿 | [`20261101/docs/技术报告_本地草稿_20261030.md`](20261101/docs/技术报告_本地草稿_20261030.md) | 离线可读，引用数字以语雀版为准 |
| 代码逐块说明 | [`20261031/docs/比赛运行代码逐块说明.md`](20261031/docs/比赛运行代码逐块说明.md) | 逐函数讲作用与由来（对应 20261029 版本） |
| 现场操作备忘 | [`20261031/docs/现场操作备忘.md`](20261031/docs/现场操作备忘.md) | 携带清单、开赛前 30 秒核对、故障速查 |
| 首轮提交物料清单 | [`20261031/docs/提交物料清单.md`](20261031/docs/提交物料清单.md) | 2026-10-30 定稿的六件套核对表 |

## 合规边界

- 随机事件只由官方 LLM 通过自然语言公告理解，不做正则、关键词匹配、固定模板或预设调度。
- 不把结构化事件参数、全图数据、本地日志或网页信息作为决策输入。
- 仓库与镜像内不含模型 API Key、席位令牌、场次 ID、地图或事件坐标；
  文档中出现的现场网关地址已脱敏为 `<现场网关>`。
- 检录录屏只出现在 `20261031/submission/`，画面不含令牌与密钥。
