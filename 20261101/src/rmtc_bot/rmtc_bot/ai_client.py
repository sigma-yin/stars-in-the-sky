import json
import os
import threading
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080/v1/chat/completions"

SYSTEM_PROMPT = (
    "你是机器人对战程序的决策助手。你只根据给定信息输出 JSON，不输出解释。"
    "JSON 格式："
    '{"avoid":[[x0,y0,x1,y1,until_tick]],'
    '"stay":[[x0,y0,x1,y1,until_tick]],'
    '"heal":[[x,y,until_tick]],'
    '"goto":[[x,y]],'
    '"attack_all_until":0,'
    '"priority":"collect|reach|survive"}。'
    "avoid 为应当避开的绝对坐标矩形（含边界），until_tick 为该区域恢复安全的 Tick；"
    "stay 为需要驻留占领的绝对坐标矩形，until_tick 为需要驻留到的 Tick；"
    "heal 只写公告明确说明进入可恢复 HP 的点位，until_tick 为该位置仍可进入的最后绝对 Tick；"
    "公告出现回血点或驻留区时，即使本车距离较远、当前也不需要，也要立即在 heal/stay 中返回，"
    "用于提前移动；只要公告说仍可进入或仍存在，就要继续保留，不要提前清空。"
    "爆炸、火线、辣椒、分裂、幻术等会造成伤害的事件坐标只能写进 avoid，绝不能写进 heal 或 stay；"
    "goto 为值得前往的绝对坐标；没有则给空数组。"
    "attack_all_until 为本车范围攻击增益有效的最后绝对 Tick，没有则为 0。"
    "公告里的相对时长必须换算成绝对 Tick；公告提到其他 robot_id 的增益时不要套用到本车。"
    "坐标均为整数，地图左上角为 [0,0]。"
)


class AIClient:
    """异步 LLM 网关客户端：不阻塞动作窗口，最多 2 个在途请求。"""

    def __init__(self, logger, seat):
        self.logger = logger
        self.url = os.environ.get("RMTC_AI_URL", DEFAULT_URL)
        self.token = os.environ.get("RMTC_TEAM_TOKEN", "")
        self.seat = seat
        # 5 s 实测扛不住现场网关：一场 19 次请求里 15 次超时。
        # 8 s 仍在结果 20 Tick（10 s）的有效期内，再长就只会拿到过期建议
        self.timeout = float(os.environ.get("RMTC_AI_TIMEOUT", "8"))
        self.no_token_warned = False
        self.model = os.environ.get("RMTC_MODEL")
        self.lock = threading.Lock()
        self.inflight = 0
        self.success = 0
        self.streak = 0
        self.last_fail = ""
        self.results = []

    def busy(self):
        with self.lock:
            return self.inflight >= 2

    def fail_streak(self):
        # 连续失败数，用来拉开重试间隔；网关限流时每 5 Tick 猛重试只会更糟
        with self.lock:
            return self.streak

    def fail_kind(self):
        # 最近一次失败的类别：timeout 说明网关只是慢，退避要短；
        # http4xx（尤其 429）才是限流，必须长退避
        with self.lock:
            return self.last_fail

    def ask(self, session, tick, prompt):
        if not self.token:
            if not self.no_token_warned:
                self.no_token_warned = True
                self.logger.warn(
                    f"RMTC_TEAM_TOKEN 未设置，座位 {self.seat} 不发送 AI 请求"
                )
            return False
        if self.busy():
            return False
        with self.lock:
            self.inflight += 1
        threading.Thread(
            target=self._request, args=(session, tick, prompt), daemon=True
        ).start()
        return True

    def take(self):
        with self.lock:
            if not self.results:
                return None
            return self.results.pop(0)

    def reset_counters(self):
        with self.lock:
            self.success = 0
            self.streak = 0

    def _request(self, session, tick, prompt):
        body = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        if self.model:
            body["model"] = self.model
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
            },
            method="POST",
        )
        began = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty content")
        except Exception as exc:
            kind = "timeout" if "timed out" in str(exc) else "error"
            with self.lock:
                self.streak += 1
                self.last_fail = kind
            self.logger.warn(
                f"AI failed at tick {tick} after {time.monotonic() - began:.1f}s "
                f"({kind}): {exc}"
            )
        else:
            with self.lock:
                self.success += 1
                self.streak = 0
                self.last_fail = ""
                self.results.append(
                    {"session": session, "tick": tick, "text": text}
                )
            self.logger.info(
                f"AI ok tick={tick} in {time.monotonic() - began:.1f}s "
                f"success={self.success}"
            )
        finally:
            with self.lock:
                self.inflight -= 1
