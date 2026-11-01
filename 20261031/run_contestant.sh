#!/usr/bin/env bash
# 启动本队参赛程序。席位、令牌、网关与 ROS 网络全部由环境变量注入，
# 不在脚本里写死任何练习令牌、场次、地图或事件参数。
set -eo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

source /opt/ros/humble/setup.bash
if [[ -f "$HERE/install/setup.bash" ]]; then
  source "$HERE/install/setup.bash"
fi

export RMTC_SEAT="${RMTC_SEAT:-A}"
export RMTC_DECISION_CONFIG="${RMTC_DECISION_CONFIG:-$HERE/decision_config.json}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-27}"
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-0}"
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"

# 容器/宿主机的 /dev/shm 不共享时共享内存传输会静默丢感知，统一改走 UDP
if [[ -f "$HERE/fastdds_udp_only.xml" ]]; then
  export FASTRTPS_DEFAULT_PROFILES_FILE="${FASTRTPS_DEFAULT_PROFILES_FILE:-$HERE/fastdds_udp_only.xml}"
fi

if [[ -z "${RMTC_TEAM_TOKEN:-}" ]]; then
  echo "缺少 RMTC_TEAM_TOKEN：请按主办方通知导入本场席位令牌后再启动。" >&2
  exit 2
fi

echo "seat=${RMTC_SEAT} domain=${ROS_DOMAIN_ID} gateway=${RMTC_AI_URL:-http://127.0.0.1:8080/v1/chat/completions}" >&2
exec ros2 run rmtc_bot bot_node
