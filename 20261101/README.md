# RMTC 2026 参赛程序 · 20261101 轮次

本目录是 2026-11-01 现场问题修补后的代码、镜像与文档快照。
上一轮（2026-10-31 递交、镜像 tag `rmtc-team:20261029`）在 [`../20261031/`](../20261031/)。

## 本轮标识

| 项 | 值 |
| --- | --- |
| 镜像标签 | `rmtc-team:20261101` |
| 镜像 ID | `sha256:21930139bd787c1d42e46dc0e5472088f416e8c0ae6aa626379a8224fe4f14a9` |
| 构建时间 | 2026-11-01 18:56:37 |
| 镜像包 | `rmtc-team-image.tar.gz`，262,526,073 字节（250.4 MiB） |
| 镜像包 sha256 | `a57b08909696bb8c53ef284327eedfe92e8f52700855c672eec244f4298ddb73` |
| `bot_node.py` | `a436aedac80972f2e50ca7bac91eee2d409c453a6dbf3eab524d7e952cfcb23b`（1749 行） |
| `ai_client.py` | `401dd63b5f289976cc0ea387039388cf838a569edb3d246b3f2f938c4c58b61a`（143 行） |
| 上一轮基线 | `bot_node.py` `c1a25510…`（1607 行）、`ai_client.py` `7eb53011…` |

镜像内的 `src/` 与本目录 [src/](src/) 逐文件 sha256 一致，可直接用 `Dockerfile` 重建出等价镜像。

## 相对上一轮改了什么

11 条改动，分两条线，外加一处新版自身的崩溃修复：

- **AI 链路容错**：超时 5 s → 8 s（可用 `RMTC_AI_TIMEOUT` 覆盖）；单个请求记录耗时与失败类别
  （timeout / error）；超时退避上限 30 Tick、限流或报错仍 60 Tick；AI 建议的时效按字段分级
  （`avoid` 20 Tick、`stay/heal/goto/attack_all_until` 40 Tick）。
- **开局与探索机动**：新增 `EDGE_TICKS=60`、`CENTER_RADIUS=2`、`CENTER_EXIT_TICKS=20`；
  开局贴边判定（距边界 ≤1 格）；寻路邻居顺序改为先横后竖且横竖都朝地图中线，贴边开局且未吃到资源时反过来；
  探索/找资源优先选「离中心不比当前更远」的格子；中心 2 格内无目标时定向离开 20 Tick；
  目标全不可达时挑周边最空的相邻格脱困，不再顶着墙 `wait`。
- **崩溃修复**：`detour_action` 把 `search_dirs()` 返回的 `(dx, dy)` 当成 `(name, (dx, dy))` 解包，
  18:13 首跑 3 分钟抛 `TypeError: cannot unpack non-iterable int object`、容器 `Exited (1)`；
  改为先建 `{增量: 方向名}` 反查表再解包。

逐条改动前因后果见 [docs/今日改动说明_20261101.md](docs/今日改动说明_20261101.md)；
10 月 31 日到 11 月 1 日的实跑数据与故障还原见
[docs/比赛报告_20261101_1524.md](docs/比赛报告_20261101_1524.md)。

## 验证状态（请勿忽略）

| 项 | 状态 |
| --- | --- |
| 语法检查（`py_compile`） | 通过 |
| 镜像内源码 = 本目录源码 | sha256 逐文件一致 |
| 脱困/贴边等新增分支的静态核对 | 已做（见改动说明 §5） |
| **真实对局** | **未验证**。18:13 那次崩溃发生在此前的中构建；修复后 18:56 重建，练习场当日未再开放，本轮全部改动没有一场实跑数据 |
| 席位 / 令牌 / 网关 / 地图 | 镜像与仓库均不含，运行时由环境变量注入 |

## 目录结构

```
20261101/
├── README.md                          # 本文件
├── Dockerfile                         # 参赛镜像构建文件
├── .dockerignore
├── run_contestant.sh                  # 启动入口（席位/令牌/网关全部来自环境变量）
├── decision_config.json               # 现场唯一允许改的参数文件（decision_type）
├── fastdds_udp_only.xml               # 容器内改走 UDP 的 DDS profile
├── src/rmtc_bot/                      # ROS 2 包
│   ├── package.xml
│   ├── setup.py / setup.cfg
│   └── rmtc_bot/
│       ├── bot_node.py                # 决策：BFS 寻路、任务/资源、事件规避、交战、脱困
│       └── ai_client.py               # 异步 LLM 网关客户端与系统提示词
├── docs/
│   ├── 今日改动说明_20261101.md        # 11 条改动 + 崩溃修复，含前因与验证口径
│   ├── 比赛报告_20261101_1524.md       # 15:24 正式轮行为还原（AI 全 401、6 次阵亡）
│   ├── 技术报告_语雀.md                # 语雀正式版链接、正文骨架、私密状态说明
│   └── 技术报告_本地草稿_20261030.md    # 10-30 本地草稿（§6/§8 未同步，仅供参考）
├── submission/                        # 递交材料（原件不入库，仅校验和与说明）
└── archive/rmtc-team-image/           # 镜像分卷（3 卷 × 90 MiB）
```

## 构建镜像

```bash
cd 20261101
docker build -t rmtc-team:20261101 .
```

镜像内已完成 `colcon build`，无需在容器内重新编译。

## 运行

席位、令牌、网关、ROS 网络均按主办方现场通知通过环境变量注入，不写死任何值：

```bash
docker run --rm --network host \
  -e RMTC_SEAT=A \
  -e RMTC_TEAM_TOKEN="<本场席位令牌>" \
  -e RMTC_AI_URL="<本场统一 LLM 网关完整 completions 地址>" \
  -e ROS_DOMAIN_ID=27 \
  -e ROS_LOCALHOST_ONLY=0 \
  -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  rmtc-team:20261101
```

可选：`-e RMTC_AI_TIMEOUT=8`（秒，默认 8）。现场网关比平时更慢时可上调，
但一条建议只在 20 Tick（500 ms/Tick 下约 10 s）内有效，超过 8 s 再等只会拿到过期建议。

- 未提供 `RMTC_TEAM_TOKEN` 时程序打印提示并以退出码 2 终止，不会空跑。
- 同 Tick 重发感知时重发缓存动作；动作回填当前感知的 `tick` 与 `session_id`。
- `decision_config.json` 每 20 Tick 重读，暂停期间切 `collect` / `survive` 后恢复即生效。

## 导出镜像文件

```bash
docker save rmtc-team:20261101 | gzip > rmtc-team-image.tar.gz
```

本仓库按 90 MiB 分卷存放，还原方式见 [archive/rmtc-team-image/README.md](archive/rmtc-team-image/README.md)。

## 合规边界

- 随机事件只由官方 LLM 通过自然语言公告理解，不做正则、关键词匹配、固定模板或预设调度。
- 不把结构化事件参数、全图数据、本地日志或网页信息作为决策输入。
- 仓库与镜像内不含任何模型 API Key、席位令牌、场次 ID、地图或事件坐标；
  文档中出现的网关地址已脱敏为 `<现场网关>`。
