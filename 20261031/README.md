# RMTC 图灵对决赛参赛程序（20261031 递交轮次）

> **本目录是首轮递交快照，对应镜像 `rmtc-team:20261029`，不再改动。**
> 11 月 1 日现场问题修补后的版本在 [`../20261101/`](../20261101/)（镜像 `rmtc-team:20261101`）。
> 下方构建与运行说明对这一轮始终有效。

ROS 2 Humble 参赛节点 `rmtc_bot`：订阅本席位感知，向本席位动作话题提交动作；
随机事件只通过官方自然语言公告交给 LLM 理解，模型输出仅做几何合法性校验。

## 目录结构

```
rmtc_ws/
├── Dockerfile                  # 参赛镜像构建文件
├── .dockerignore
├── run_contestant.sh           # 启动入口（环境变量注入席位/令牌/网关）
├── decision_config.json        # 现场唯一允许修改的参数文件
├── src/rmtc_bot/
│   ├── package.xml
│   ├── setup.py
│   └── rmtc_bot/
│       ├── bot_node.py         # 决策：BFS 寻路、任务/资源、事件规避、交战
│       └── ai_client.py        # 异步 LLM 网关客户端与系统提示词
└── backup/                     # 第一阶段冻结版本（不参与镜像）
```

## 构建镜像

```bash
cd rmtc_ws
docker build -t rmtc-team:latest .
```

镜像内已完成 `colcon build`，无需在容器内重新编译。

## 运行

席位、令牌、网关、ROS 网络均按主办方现场通知通过环境变量注入：

```bash
docker run --rm --network host \
  -e RMTC_SEAT=A \
  -e RMTC_TEAM_TOKEN="<本场席位令牌>" \
  -e RMTC_AI_URL="<本场统一 LLM 网关完整 completions 地址>" \
  -e ROS_DOMAIN_ID=27 \
  -e ROS_LOCALHOST_ONLY=0 \
  -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  rmtc-team:latest
```

不使用 Docker 时：

```bash
export RMTC_SEAT=A
export RMTC_TEAM_TOKEN="<本场席位令牌>"
export RMTC_AI_URL="<本场统一 LLM 网关完整 completions 地址>"
export ROS_DOMAIN_ID=27
export ROS_LOCALHOST_ONLY=0
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
./run_contestant.sh
```

- `RMTC_AI_URL` 未设置时默认 `http://127.0.0.1:8080/v1/chat/completions`（本机练习代理），
  正式比赛必须设置为主办方通知的网关地址。
- 程序不读取任何密钥文件，不发送模型密钥；令牌仅用于本地网关注入。
- 动作回填当前感知的 `tick` 与 `session_id`；同 Tick 重发感知时重发缓存动作。
- 镜像内默认使用 `fastdds_udp_only.xml`：容器与宿主机 `/dev/shm` 不共享时，Fast DDS 的共享内存
  传输会静默丢感知，改走 UDP 后可跨容器/跨机稳定收发。

## 导出镜像文件

```bash
docker save rmtc-team:latest | gzip > rmtc-team-image.tar.gz
```

约 1.1 GB（压缩后更小）。也可直接提交 `Dockerfile` 与 `src/`，由赛方自行构建。

## 决策类型

`decision_config.json` 只包含 `decision_type`，现场允许在暂停期间切换：

- `collect`：采集模式。完成三项任务后继续收集资源，伤害未满时在胜算足够时交战。
- `survive`：生存模式。不做主动进攻，优先躲避敌人与危险区域。

程序每 20 Tick 重新读取该文件，暂停期间修改、恢复后生效。

## 日志

节点日志输出到标准错误，包含每 Tick 的 `pos/hp/res/send`、AI 请求结果与状态切换。
需要排查时在 `docker run` 上加 `2>&1 | tee bot.log`。

## 备注

- Stage 1（100 Tick、100 ms/Tick）与 Stage 2（500 Tick、500 ms/Tick）共用同一节点，
  通过感知字段自动适配，无需切换参数。
- 练习包中的本地令牌、场次 ID、地图、事件时间与任务坐标仅用于赛前联调，
  未写入程序与镜像。
