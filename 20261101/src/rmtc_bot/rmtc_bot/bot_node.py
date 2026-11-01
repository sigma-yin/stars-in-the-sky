import json
import os
import time
from collections import deque

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from std_msgs.msg import String

from .ai_client import AIClient

SEAT = os.environ.get("RMTC_SEAT", "A")
DUMP = os.environ.get("RMTC_DUMP")

QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
)

DIRS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}
RESOURCE_TYPES = ("resource_normal", "resource_high")
# 有效伤害目标是「平局保险 700 + 一次死亡余量 60 = 760」。
# D≥500 就拿满对抗项，700 以上只为同分排序；死亡一次倒扣 60，
# 停火线必须比目标高 60，否则终局一死就跌回 700 以下（1000 实测只多换
# 1~2 次死亡，每次 -60，性价比为负）。
# 两条分支不再有差别：R2 是 50 还是 60/70 都按同一套 700 保险线 + 30 HP 闸门跑。
# 曾给 R2>50 单独设过 50 HP 闸门，实测只是让 bot 在 41~50 HP 逛街，D 掉到
# 330~585、死亡反而涨到 6~8，对分数和排序都是负收益。
RESOURCE_TARGET = 60
DAMAGE_FLOOR = 700
DAMAGE_TARGET = DAMAGE_FLOOR + 60
SPRINT_TICKS = 300
LATE_TICKS = 350
# 出生点贴边时，开局这么多 Tick 内改走竖向（把车先带离边角）
EDGE_TICKS = 60
# 中心附近既没资源也没敌人时的离心动作为：判定半径 + 至少坚持的 Tick 数
CENTER_RADIUS = 2
CENTER_EXIT_TICKS = 20
# 1v1 抢资源判定半径：几步之内的资源点才算同一场争夺，太远就各走各的
CONTEST_RANGE = 4
MAX_AI_UNTIL = 80
MAX_AVOID_RECTS = 6
MAX_RECT_RATIO = 0.25
MAX_AVOID_RATIO = 0.40
MAX_HEAL_POINTS = 4


def relative_position(text):
    """9x9 相对位置字符串 -> (dx, dy)。"""
    if not isinstance(text, str):
        return None
    rows = text.splitlines()
    if len(rows) != 9 or any(len(row) != 9 for row in rows):
        return None
    for y, row in enumerate(rows):
        x = row.find("2")
        if x >= 0:
            return x - 4, y - 4
    return 0, 0


def parse_json_object(text):
    """从模型输出里截取第一个 JSON 对象。"""
    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def as_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_rects(
    items,
    tick,
    width=None,
    height=None,
    max_count=MAX_AVOID_RECTS,
    max_total_ratio=MAX_AVOID_RATIO,
):
    rects = []
    if not isinstance(items, list):
        return rects
    rect_cap = None
    total_cap = None
    if width and height:
        rect_cap = width * height * MAX_RECT_RATIO
        total_cap = width * height * max_total_ratio
    covered = set()
    for item in items:
        if isinstance(item, dict):
            rect = item.get("rect")
            until = as_int(item.get("until", item.get("until_tick")))
        elif isinstance(item, list):
            rect = item[:4]
            until = as_int(item[4]) if len(item) >= 5 else None
        else:
            continue
        if not isinstance(rect, list) or len(rect) < 4:
            continue
        coords = [as_int(v) for v in rect[:4]]
        if any(v is None for v in coords):
            continue
        if width and height:
            x0, x1 = sorted((coords[0], coords[2]))
            y0, y1 = sorted((coords[1], coords[3]))
            # 模型偶尔给出越界或过大的区域，整条丢弃，避免锁死全图
            if x0 < 0 or y0 < 0 or x1 >= width or y1 >= height:
                continue
            if (x1 - x0 + 1) * (y1 - y0 + 1) > rect_cap:
                continue
        if until is None:
            until = tick + 40
        until = max(tick, min(until, tick + MAX_AI_UNTIL))
        if total_cap is not None:
            cells = {
                (x, y)
                for x in range(min(coords[0], coords[2]), max(coords[0], coords[2]) + 1)
                for y in range(min(coords[1], coords[3]), max(coords[1], coords[3]) + 1)
            }
            if len(covered | cells) > total_cap:
                continue
            covered |= cells
        rects.append((*coords, until))
        if len(rects) >= max_count:
            break
    return rects


def parse_points(items):
    points = []
    if not isinstance(items, list):
        return points
    for item in items:
        if isinstance(item, dict):
            item = item.get("position")
        if not isinstance(item, list) or len(item) < 2:
            continue
        x, y = as_int(item[0]), as_int(item[1])
        if x is None or y is None:
            continue
        points.append((x, y))
    return points


def parse_heal(items, tick, width=None, height=None):
    """回血点列表 -> (x, y, 最后可进入 Tick)。"""
    points = []
    if not isinstance(items, list):
        return points
    for item in items:
        if isinstance(item, dict):
            pos = item.get("position") or item.get("point")
            until = as_int(item.get("until", item.get("until_tick")))
        elif isinstance(item, list):
            pos = item[:2]
            until = as_int(item[2]) if len(item) >= 3 else None
        else:
            continue
        if not isinstance(pos, list) or len(pos) < 2:
            continue
        x, y = as_int(pos[0]), as_int(pos[1])
        if x is None or y is None:
            continue
        if width and height and not (0 <= x < width and 0 <= y < height):
            continue
        if until is None:
            until = tick + 20
        until = max(tick, min(until, tick + MAX_AI_UNTIL))
        points.append((x, y, until))
        if len(points) >= MAX_HEAL_POINTS:
            break
    return points


class BotNode(Node):
    def __init__(self):
        super().__init__("rmtc_bot")
        self.pub = self.create_publisher(String, f"/team_{SEAT}/action", QOS)
        self.create_subscription(
            String, f"/team_{SEAT}/perception", self.on_perception, QOS
        )
        self.ai = AIClient(self.get_logger(), SEAT)
        self.decision_path = os.environ.get(
            "RMTC_DECISION_CONFIG", "decision_config.json"
        )
        self.last_session = None
        self.foreign_session = None
        self.last_seen = 0.0
        self.last_tick = None
        self.last_action = None
        self.width = 20
        self.height = 20
        self.reset_session()
        self.get_logger().info(f"rmtc_bot ready, seat={SEAT}")

    def reset_session(self):
        self.decision_type = "collect"
        self.last_decision_check = -1000
        self.me = None
        self.prev_pos = None
        self.urgent = False
        self.hunting = False
        # 压制防御目标时允许踩进它的火线：防御当 Tick 不会还手
        self.pressing = False
        self.free = set()
        self.blocked = set()
        # E_BLOCKED 是单帧结果（别的车同 Tick 抢格/临时占位），不能当成永久障碍，
        # 否则一次失败就把可达格写死，reach 点被毒掉后整场都踩不到
        self.blocked_until = {}
        self.resources = {}
        self.enemies = {}
        self.enemy_seen = {}
        # 敌车上一步位移 + 观测 Tick，用来外推拦截点；只信 2 Tick 内的观测
        self.enemy_vel = {}
        self.missions = []
        self.completed_ids = set()
        self.event_len = 0
        self.avoid = []
        self.stay = []
        self.heal = []
        self.goto = []
        self.attack_all_until = 0
        self.bastion_ticks = 0
        self.edge_checked = False
        self.edge_open = False
        self.first_resource_tick = None
        self.exit_dir = None
        self.exit_until = 0
        self.hp = 100
        self.protected = False
        self.ai_started = False
        self.last_ai_tick = -1000
        self.avoid_cells = set()
        self.score_resource = 0
        self.score_damage = 0
        self.combat_done = False
        self.pending = None
        self.pending_event = ""
        self.asked_event = ""
        self.ask_success_base = 0
        self.ai.reset_counters()

    def load_decision(self):
        try:
            with open(self.decision_path, encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(obj, dict):
            return
        value = obj.get("decision_type")
        if value in ("collect", "survive") and value != self.decision_type:
            self.decision_type = value
            self.get_logger().info(f"decision_type={value}")

    def in_bounds(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height

    def on_perception(self, msg):
        try:
            p = json.loads(msg.data)
            tick = int(p["tick"])
            session = str(p["session_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self.get_logger().warn(f"bad perception: {exc}")
            return
        if DUMP:
            with open(DUMP, "a", encoding="utf-8") as f:
                f.write(msg.data + "\n")

        if session != self.last_session:
            # 同域若混入其他裁判（练习局域网常见），中途冒出的新场次 Tick 已经很大，
            # 跟随它会让本场动作全部 session_mismatch；重开/重启都从 Tick 0 开始，
            # 所以只接受 Tick 很小的新场次。原场次 3 秒没有新感知则视为裁判已重开，
            # 避免我车晚接入时错过 Tick 0~5。
            if (
                self.last_session is not None
                and tick > 5
                and time.monotonic() - self.last_seen < 3.0
            ):
                if session != self.foreign_session:
                    self.foreign_session = session
                    self.get_logger().warn(
                        f"忽略异常场次 {session} tick={tick}，疑似同域其他裁判"
                    )
                return
            self.last_session = session
            self.foreign_session = None
            self.last_tick = None
            self.last_action = None
            self.reset_session()
            self.load_decision()
            self.last_decision_check = tick
            self.get_logger().info(f"new session: {session}")

        # 规则允许暂停期间改 decision_type 并在恢复后生效，定期重读配置
        if tick - self.last_decision_check >= 20:
            self.last_decision_check = tick
            self.load_decision()

        if self.last_tick is not None and tick < self.last_tick:
            return
        if self.last_tick == tick:
            if self.last_action is not None:
                self.pub.publish(String(data=self.last_action))
            return

        self.update_world(p)

        if self.missions:
            self.apply_ai(tick)
            self.maybe_ask_ai(p, tick, session)
        self.avoid_cells = self.hazard_cells(tick)
        self.last_tick = tick
        self.last_seen = time.monotonic()
        action = self.avoid_backtrack(self.decide(p))
        payload = {"tick": tick, "session_id": session, **action}
        self.last_action = json.dumps(payload, ensure_ascii=False)
        self.pub.publish(String(data=self.last_action))
        self.pending = self.make_pending(p, action, tick)
        self.get_logger().info(
            f"tick={tick} pos={self.me} hp={self.hp} "
            f"res={len(self.resources)} send={action}"
        )

    def update_world(self, p):
        now = as_int(p.get("tick")) or 0
        self.prev_pos = self.me
        me = p.get("self") or {}
        pos = me.get("position")
        if isinstance(pos, list) and len(pos) >= 2:
            x, y = as_int(pos[0]), as_int(pos[1])
            if x is not None and y is not None:
                self.me = (x, y)
                self.free.add(self.me)
            else:
                self.me = None
        else:
            self.me = None

        self.hp = as_int(me.get("hp")) or 0
        self.protected = bool(me.get("protected"))

        m = p.get("map") or {}
        try:
            self.width = int(m.get("width", self.width))
            self.height = int(m.get("height", self.height))
        except (TypeError, ValueError):
            pass

        # 出生点贴边（距边界 2 格以内）时，开局前 EDGE_TICKS 个 Tick 改走竖向，
        # 先把车从边角带出来；只在开局判定一次，之后不再覆盖横向前提
        if self.me is not None and not self.edge_checked:
            self.edge_checked = True
            self.edge_open = (
                self.me[0] <= 1
                or self.me[1] <= 1
                or self.me[0] >= self.width - 2
                or self.me[1] >= self.height - 2
            )
            if self.edge_open:
                self.get_logger().info(
                    f"开局贴边 pos={self.me} size=({self.width}x{self.height}) "
                    f"前 {EDGE_TICKS} Tick 优先竖向"
                )

        missions = p.get("missions")
        if isinstance(missions, list):
            self.missions = [x for x in missions if isinstance(x, dict)]
        score = p.get("score") or {}
        self.score_resource = as_int(score.get("resource_value")) or 0
        # 吃到第一个资源后就结束开局竖向模式：改回横向前进，边横移边继续收资源
        if self.first_resource_tick is None and self.score_resource > 0:
            self.first_resource_tick = now
            self.get_logger().info(
                f"首个资源到手 tick={now} 资源分={self.score_resource}，"
                "结束开局竖向、改横向前进"
            )
        self.score_damage = as_float(score.get("effective_damage")) or 0.0
        # 死亡会倒扣有效伤害，D 可能跌破 500，所以每帧重算而不是一置 True 就不回头
        self.combat_done = self.score_damage >= DAMAGE_TARGET
        ids = score.get("mission_completed_ids")
        if isinstance(ids, list):
            self.completed_ids = {str(x) for x in ids}

        last = me.get("last_action") or {}
        if self.pending is not None:
            if last.get("tick") == self.pending["tick"]:
                if last.get("moved") and self.me is not None:
                    self.blocked.discard(self.me)
                    self.blocked_until.pop(self.me, None)
                elif not last.get("moved") and last.get("error") in (
                    "E_BLOCKED",
                    "E_OOB",
                ):
                    fx, fy = self.pending["from"]
                    dx, dy = DIRS[self.pending["direction"]]
                    cell = (fx + dx, fy + dy)
                    if self.in_bounds(*cell):
                        self.blocked_until[cell] = (self.last_tick or 0) + 60
                        self.free.discard(cell)
                        self.get_logger().info(f"blocked {cell} ({last.get('error')})")
            self.pending = None

        if self.me is None:
            self.enemies = {}
            return

        self.enemies = {}
        px, py = self.me
        visible_resources = set()
        for ent in p.get("visible_entities") or []:
            rel = relative_position(ent.get("position", ""))
            if rel is None:
                continue
            cell = (px + rel[0], py + rel[1])
            if not self.in_bounds(*cell):
                continue
            kind = ent.get("type")
            if kind == "obstacle":
                self.blocked.add(cell)
                self.free.discard(cell)
            elif kind in RESOURCE_TYPES:
                self.resources[cell] = {
                    "type": kind,
                    "value": as_int(ent.get("value")) or 0,
                    "seen": now,
                }
                visible_resources.add(cell)
                self.free.add(cell)
            elif kind == "robot":
                rid = str(
                    ent.get("robot_id") or ent.get("id") or f"{cell[0]},{cell[1]}"
                )
                # 车能站上去就不是静态障碍：清掉冲突记忆，等它走开这格又能走
                self.blocked.discard(cell)
                self.blocked_until.pop(cell, None)
                self.enemies[cell] = {
                    "hp": as_int(ent.get("hp")) or 0,
                    "protected": bool(ent.get("protected")),
                    "defending": bool(ent.get("defending")),
                    "id": rid,
                }
                # 记住最后一次看到的位置：视野里没敌人时朝那里找，增加接战率
                prev = self.enemy_seen.get(rid)
                if prev is not None:
                    dt = now - int(prev["tick"])
                    dx = cell[0] - prev["cell"][0]
                    dy = cell[1] - prev["cell"][1]
                    if 0 < dt <= 2 and abs(dx) + abs(dy) == 1:
                        self.enemy_vel[rid] = (dx, dy)
                    else:
                        self.enemy_vel.pop(rid, None)
                self.enemy_seen[rid] = {
                    "cell": cell,
                    "tick": now,
                    "hp": as_int(ent.get("hp")) or 0,
                }

        for rid, info in list(self.enemy_seen.items()):
            if now - int(info["tick"]) > 70:
                del self.enemy_seen[rid]
                self.enemy_vel.pop(rid, None)

        for cell, until in list(self.blocked_until.items()):
            if until < now:
                del self.blocked_until[cell]

        vision = as_int(p.get("vision_radius"))
        if vision is None:
            vision = 4
        for cell in list(self.resources):
            if cell in self.blocked:
                del self.resources[cell]
                continue
            # 视野内没看到资源就是被别人拿走了；视野外保留记忆继续当目标
            if (
                max(abs(cell[0] - px), abs(cell[1] - py)) <= vision
                and cell not in visible_resources
            ):
                del self.resources[cell]

        # 任务未完成时久未看到的资源仍按时间丢弃，避免为不确定的目标绕远；
        # 任务全部完成后资源就是第二目标，只按视野清理，不再自然遗忘
        if self.unfinished_missions():
            for cell, info in list(self.resources.items()):
                if now - int(info.get("seen", now)) > 120:
                    del self.resources[cell]

    def hazard_cells(self, tick):
        cells = set()
        living = []
        for x0, y0, x1, y1, until in self.avoid:
            if until < tick:
                continue
            living.append((x0, y0, x1, y1, until))
            for x in range(max(0, min(x0, x1)), min(self.width - 1, max(x0, x1)) + 1):
                for y in range(max(0, min(y0, y1)), min(self.height - 1, max(y0, y1)) + 1):
                    cells.add((x, y))
        self.avoid = living
        return cells

    def maybe_ask_ai(self, p, tick, session):
        text = p.get("event_text")
        if not isinstance(text, str):
            text = ""
        if len(text) < self.event_len:
            self.event_len = 0
            self.pending_event = ""
            self.asked_event = ""

        delta = text[self.event_len :].strip()
        if delta:
            self.pending_event = (
                self.pending_event + "\n" + delta
            ).strip()[-2000:]
            self.event_len = len(text)

        # 请求成功后才把已问过的事件从待处理列表移除；失败则保留并重试
        if self.asked_event and self.ai.success > self.ask_success_base:
            if self.pending_event.startswith(self.asked_event):
                self.pending_event = self.pending_event[len(self.asked_event) :].strip()
            else:
                self.pending_event = ""
            self.asked_event = ""

        need = (
            bool(self.pending_event)
            or not self.ai_started
            or self.ai.success == 0
        )
        if not need:
            return

        interval = 5 if delta or self.asked_event else 20
        # 连败退避分两类：网关只是慢（timeout）时 8 s 超时本身已占 16 Tick，
        # 再按 30 s 退避整场只能试个位数；只有被限流/报错才需要长退避
        cap = 30 if self.ai.fail_kind() == "timeout" else 60
        interval = max(interval, min(cap, 10 * self.ai.fail_streak()))
        if tick - self.last_ai_tick < interval:
            return

        prompt = self.build_prompt(p, tick, self.pending_event)
        if not self.ai.ask(session, tick, prompt):
            return
        self.ai_started = True
        self.last_ai_tick = tick
        self.asked_event = self.pending_event
        self.ask_success_base = self.ai.success

    def build_prompt(self, p, tick, delta):
        me = p.get("self") or {}
        robot_id = me.get("robot_id") or me.get("team") or SEAT
        mission_text = p.get("mission_text") or ""
        missions = json.dumps(self.missions, ensure_ascii=False)
        done = ",".join(sorted(self.completed_ids)) or "无"
        if delta:
            event_part = delta
        else:
            event_part = "（无新公告，请只根据任务给出值得探索的坐标）"
        return (
            f"当前 Tick：{tick}\n"
            f"本车 robot_id：{robot_id}，座位：{SEAT}\n"
            f"地图尺寸：{self.width}x{self.height}，坐标左上角为 [0,0]\n"
            f"本车位置：{me.get('position')}，HP：{me.get('hp')}\n"
            f"任务描述：{mission_text}\n"
            f"任务结构：{missions}\n"
            f"已完成任务：{done}\n"
            f"最新公告：\n{event_part}\n"
            "提醒：公告里只要出现可回血点或需要驻留的区域（包括预告、尚未生效），"
            "就立刻在 heal/stay 中给出绝对坐标和最后 Tick，即使本车很远、当前不需要也要给；"
            "这些点仍在有效期内就继续保留，不要用空数组清空。"
            "但要区分事件性质：只有明确回血或需要驻留的区域才写 heal/stay，"
            "爆炸、火线、辣椒、分裂等伤害事件坐标只能写 avoid，不能写进 heal/stay。\n"
        )

    def apply_ai(self, tick):
        while True:
            res = self.ai.take()
            if res is None:
                return
            if res["session"] != self.last_session:
                continue
            age = tick - res["tick"]
            # 按字段分级过期：avoid 和当前位置强相关，20 Tick（10 s）以外的不能再用；
            # heal/stay/goto/attack_all 自带 until_tick，迟到 40 Tick 也还能提前移动
            if age > 40:
                continue
            obj = parse_json_object(res["text"])
            if obj is None:
                self.get_logger().warn(f"AI output not JSON tick={res['tick']}")
                continue
            if age <= 20:
                fresh = parse_rects(
                    obj.get("avoid"), tick, self.width, self.height
                )
                self.avoid = [r for r in self.avoid if r[4] >= tick] + fresh
            if "stay" in obj:
                fresh_stay = parse_rects(
                    obj.get("stay"),
                    tick,
                    self.width,
                    self.height,
                    max_count=2,
                    max_total_ratio=0.5,
                )
                # 驻留意图自带 until_tick；模型偶尔回空数组不该让 bot 提前离场
                if fresh_stay:
                    self.stay = fresh_stay
            if "heal" in obj:
                fresh_heal = parse_heal(
                    obj.get("heal"), tick, self.width, self.height
                )
                # 回血点同样自带 until_tick；模型回空数组不清空仍在有效期内的点位
                if fresh_heal:
                    self.heal = fresh_heal
            if "attack_all_until" in obj:
                until = as_int(obj.get("attack_all_until"))
                # 截止 Tick 只增不减：模型偶尔回 0 不代表增益提前结束，
                # 覆盖成 0 会白丢整个无双攻击窗口
                if until is not None and until > self.attack_all_until:
                    self.attack_all_until = max(
                        0, min(until, tick + MAX_AI_UNTIL)
                    )
            self.goto = parse_points(obj.get("goto"))
            self.get_logger().info(
                f"AI advice tick={tick} avoid={self.avoid} stay={self.stay} "
                f"heal={self.heal} attack_all_until={self.attack_all_until} "
                f"goto={self.goto}"
            )

    def make_pending(self, p, action, tick):
        if action.get("action") != "move" or self.me is None:
            return None
        return {
            "from": self.me,
            "direction": action["direction"],
            "tick": tick,
        }

    def reach_mission(self):
        for mission in self.missions:
            if mission.get("type") != "reach":
                continue
            if str(mission.get("id")) in self.completed_ids:
                return None
            pos = mission.get("position")
            if isinstance(pos, list) and len(pos) >= 2:
                x, y = as_int(pos[0]), as_int(pos[1])
                if x is not None and y is not None and self.in_bounds(x, y):
                    return (x, y)
        return None

    def wanted_resource_types(self):
        need = set()
        for mission in self.missions:
            if str(mission.get("id")) in self.completed_ids:
                continue
            kind = mission.get("type")
            if kind == "normal_resources":
                need.add("resource_normal")
            elif kind == "high_resources":
                need.add("resource_high")
            elif kind == "resource_value":
                need.update(RESOURCE_TYPES)
        return need

    def attack_candidates(self):
        """每个方向的最近敌车：(距离, 方向, 格子, 实体)。"""

        out = []
        if self.me is None:
            return out
        for name, (dx, dy) in DIRS.items():
            for step in (1, 2):
                cell = (self.me[0] + dx * step, self.me[1] + dy * step)
                if not self.in_bounds(*cell):
                    break
                ent = self.enemies.get(cell)
                if ent is not None:
                    out.append((step, name, cell, ent))
                    break
        return out

    def nearest_enemy_in_line(self):
        best = None
        for step, name, cell, ent in self.attack_candidates():
            if ent["protected"]:
                continue
            if best is None or step < best[0]:
                best = (step, name, ent)
        return best

    def aoe_target(self):
        if self.me is None:
            return None
        x0, y0 = self.me
        best = None
        for cell, ent in self.enemies.items():
            if ent["protected"] or ent["defending"]:
                continue
            dx = abs(cell[0] - x0)
            dy = abs(cell[1] - y0)
            if dx > 2 or dy > 2:
                continue
            if best is None or dx + dy < best[0]:
                best = (dx + dy, cell)
        return best

    def aoe_action(self, tick):
        if self.attack_all_until <= 0 or tick > self.attack_all_until:
            return None
        if self.protected or self.combat_done or self.hp <= 20:
            return None
        hit = self.aoe_target()
        if hit is None:
            return None
        cell = hit[1]
        dx = cell[0] - self.me[0]
        dy = cell[1] - self.me[1]
        if abs(dx) >= abs(dy) and dx != 0:
            direction = "right" if dx > 0 else "left"
        elif dy != 0:
            direction = "down" if dy > 0 else "up"
        else:
            return None
        return {"action": "attack", "direction": direction}

    def threat_count(self, cell, skip=None):
        # 站到该格会被几个敌人打到（出生保护中的敌人不能攻击）；
        # 普通攻击沿正交方向 1~2 格，skip 用来忽略指定敌人（狩猎目标）
        count = 0
        for pos, ent in self.enemies.items():
            if ent["protected"] or pos == skip:
                continue
            if pos[0] == cell[0] and abs(pos[1] - cell[1]) <= 2:
                count += 1
            elif pos[1] == cell[1] and abs(pos[0] - cell[0]) <= 2:
                count += 1
        return count

    def escape_key(self, cell):
        nearest = min(
            abs(cell[0] - ex) + abs(cell[1] - ey) for ex, ey in self.enemies
        )
        return (self.threat_count(cell), -nearest)

    def traversable(self, cell):
        # 脱险专用：允许穿过火线（这一 Tick 的伤害早已按移动前位置结算），
        # 只避开越界、障碍、当前敌车和生效中的告警区
        if not self.in_bounds(*cell):
            return False
        if cell in self.blocked or cell in self.enemies:
            return False
        return cell not in self.avoid_cells

    def escape_action(self):
        """多步脱险：在 5 步内找威胁源更少、离敌人更远的格子。

        只走一步常常仍在另一个敌人的火线里（十字夹击），所以按可达范围内
        最优格反向取路径；没有更安全的格子就返回 None，交给防御减半。
        """

        if self.me is None or not self.enemies:
            return None
        start = self.me
        start_key = self.escape_key(start)
        if start_key[0] == 0:
            return None
        dist = {start: 0}
        prev = {start: None}
        queue = deque([start])
        best_cell = None
        best_key = start_key
        while queue:
            cur = queue.popleft()
            if dist[cur] >= 5:
                continue
            for dx, dy in DIRS.values():
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in dist or not self.traversable(nxt):
                    continue
                dist[nxt] = dist[cur] + 1
                prev[nxt] = cur
                queue.append(nxt)
                key = self.escape_key(nxt)
                if key < best_key:
                    best_key = key
                    best_cell = nxt
        if best_cell is None:
            return None
        self.urgent = True
        return self.step_action(self.reconstruct(prev, best_cell))

    def damage_locked(self):
        # 平局保险的 700 已到手：不再开新战，只把手上这一场打完到停火线 760。
        # 死亡一次 -60，多打一场的收益抵不上一次死亡，之后优先压死亡数
        return self.score_damage >= DAMAGE_FLOOR

    def catch_up(self):
        # 尾盘伤害还差一大截：连满血目标也得接，不能再挑食。
        # 实测有场次 223~379 Tick 一直 40~60 HP、场上没残血，白逛 156 Tick，D 只有 410
        return (
            (self.last_tick or 0) >= LATE_TICKS
            and self.score_damage < DAMAGE_TARGET
        )

    def hunt_floor(self):
        # 统一 30 HP 下限：R2>50 时抬到 50 实测只让 bot 在 41~50 HP 逛街不出手
        # （四场 D 只有 330/470/585、死亡反而 6~8），比激进分支更差。
        # 抬太高同样不行：没有回血点时 bot 长期停在 40~60 HP，阈值 70 会把半个
        # 赛程的接战全禁掉（实测 D 从 560 掉到 390）。
        # 尾盘伤害还差一大截时再降到 20 HP：只剩百来个 Tick，保守就来不及
        if self.catch_up():
            return 20
        return 30

    def fight_to_death(self):
        # 尾盘 700 还没到手：20 HP 也不再守点。每条命 100 HP 能换 5 枪，
        # 死一次 -60，净赚约 40，站着守点反而白丢这 100 点潜在伤害
        # （实测 20 HP 守点空窗 120 Tick、D 卡在 650）
        return (
            (self.last_tick or 0) >= LATE_TICKS
            and self.score_damage < DAMAGE_FLOOR
        )

    def should_hunt(self):
        # 低血不主动找人；30~60 只追残血目标；60 以上可以主动求战。
        # 平局保险 700 到手后同样只追残血，不再跟满血对手换血
        if self.combat_done:
            return False
        if self.hp <= self.hunt_floor() and not self.fight_to_death():
            return False
        if (self.hp > 60 or self.catch_up()) and not self.damage_locked():
            return bool(self.enemies)
        return any(
            not ent["protected"] and not ent["defending"] and ent["hp"] <= 40
            for ent in self.enemies.values()
        )

    def hunt_enemies(self):
        if not self.enemies:
            return None
        if self.combat_done or (self.hp <= 20 and not self.fight_to_death()):
            return None
        tick = self.last_tick or 0
        aoe = 0 < self.attack_all_until and tick <= self.attack_all_until
        # 血量不健康、或平局保险已到手时只去追残血目标，别主动招惹满血敌人；
        # 尾盘落后时例外：没时间挑食了
        low = self.damage_locked() or (self.hp <= 60 and not self.catch_up())
        goals = set()
        self.hunting = True
        try:
            for (ex, ey), ent in self.enemies.items():
                if ent["protected"] or ent["defending"]:
                    continue
                if low and ent["hp"] > 40:
                    continue
                # 追逃不追屁股：按敌车上一步位移算出它的落点再堵
                ex, ey = self.intercept_point((ex, ey), ent)
                for x in range(max(0, ex - 2), min(self.width - 1, ex + 2) + 1):
                    for y in range(max(0, ey - 2), min(self.height - 1, ey + 2) + 1):
                        cell = (x, y)
                        if not self.passable(cell):
                            continue
                        # 低血/保险到手后不站交叉火力；满血时该进就进，
                        # 伤害才是目标，一味躲火力会整场不接战（实测 D 232）
                        if (
                            (self.hp <= 60 or self.damage_locked())
                            and not aoe
                            and self.threat_count(cell) > 1
                        ):
                            continue
                        goals.add(cell)
            if not goals:
                return None
            path = self.bfs_to_any(self.me, goals)
        finally:
            self.hunting = False
        return self.next_move(path)

    def intercept_point(self, cell, ent):
        """按敌车上一步位移外推落点；迎面来的不预判，绕到前面反而更远。"""
        vel = self.enemy_vel.get(ent.get("id"))
        if vel is None or self.me is None:
            return cell
        vx, vy = vel
        # 只有它正在远离我们时才预判
        if (cell[0] - self.me[0]) * vx + (cell[1] - self.me[1]) * vy <= 0:
            return cell
        gap = abs(cell[0] - self.me[0]) + abs(cell[1] - self.me[1])
        steps = 2 if gap >= 6 else 1
        return (
            min(max(0, cell[0] + vx * steps), self.width - 1),
            min(max(0, cell[1] + vy * steps), self.height - 1),
        )

    def pressure_move(self):
        """40~60 HP 又打不动眼前目标时别漫游，先占好下一枪的位置。

        残血且正在防御的敌人：站进它的正交 1~2 格。防御当 Tick 不攻击，
        站在火线里也不会挨打，反而每 Tick 都能白打 10 点，4 枪补掉。
        其余残血敌：占预判落点的侧翼，等它自己走进射程。
        满血敌不贴：贴上去也打不动，只会跟着它绕圈（实测 50 Tick 零开火）。
        """
        if self.me is None or self.damage_locked() or self.catch_up():
            return None
        if not self.hunt_floor() < self.hp <= 60:
            return None
        goals = set()
        self.pressing = True
        try:
            for (ex, ey), ent in self.enemies.items():
                if ent["protected"]:
                    continue
                if ent["defending"]:
                    # 只卡两枪能补掉的防御目标，其他交给正常巡逻
                    if ent["hp"] > 20:
                        continue
                    offsets = (
                        (0, -1),
                        (0, 1),
                        (-1, 0),
                        (1, 0),
                        (0, -2),
                        (0, 2),
                        (-2, 0),
                        (2, 0),
                    )
                else:
                    if ent["hp"] > 60:
                        continue
                    ex, ey = self.intercept_point((ex, ey), ent)
                    offsets = (
                        (-2, -1),
                        (2, 1),
                        (-2, 1),
                        (2, -1),
                        (-1, -2),
                        (1, 2),
                        (-1, 2),
                        (1, -2),
                    )
                for dx, dy in offsets:
                    cell = (ex + dx, ey + dy)
                    if self.in_bounds(*cell) and self.passable(cell):
                        goals.add(cell)
            if not goals:
                return None
            path = self.bfs_to_any(self.me, goals)
        finally:
            self.pressing = False
        return self.next_move(path)

    def hunt_memory(self):
        # 视野里没有敌人时，朝 60 Tick 内最后看到的位置找，减少空转逛街。
        # 阈值跟狩猎下限一致：30 HP 时若也禁追，bot 会在地图中央原地打转，
        # 实测 325~500 Tick 一次没出手，D 冻结在 232。
        # 平局保险到手后不再追记忆点：那边多半是个满血对手，死了就是 -60
        if (
            self.me is None
            or self.combat_done
            or self.damage_locked()
            or (self.hp <= self.hunt_floor() and not self.fight_to_death())
        ):
            return None
        tick = self.last_tick or 0
        goals = {
            info["cell"]
            for info in self.enemy_seen.values()
            if tick - int(info["tick"]) <= 60 and self.passable(info["cell"])
        }
        if not goals:
            return None
        return self.next_move(self.bfs_to_any(self.me, goals))

    @staticmethod
    def inside_rect(cell, rect):
        x0, y0, x1, y1, _ = rect
        return (
            min(x0, x1) <= cell[0] <= max(x0, x1)
            and min(y0, y1) <= cell[1] <= max(y0, y1)
        )

    def stay_action(self, tick):
        rects = [r for r in self.stay if r[4] >= tick]
        if not rects:
            return None
        if self.hp <= 50 and self.nearest_enemy_in_line() is not None:
            return None
        # 驻留期间若已经有可开火目标，交给交战逻辑；在区域内往返会白丢伤害
        if any(
            not cand[3]["protected"] and not cand[3]["defending"]
            for cand in self.attack_candidates()
        ):
            return None
        here = self.me
        current = [r for r in rects if self.inside_rect(here, r)]
        if current:
            # 驻留要求移动后仍在区域内，所以在区域内往返而不是原地等待；
            # 优先向区域中心靠拢，5×5 范围攻击的覆盖面积最大
            x0, y0, x1, y1, _ = current[0]
            cx = (min(x0, x1) + max(x0, x1)) // 2
            cy = (min(y0, y1) + max(y0, y1)) // 2
            best = None
            for name, (dx, dy) in DIRS.items():
                cell = (here[0] + dx, here[1] + dy)
                if not self.passable(cell):
                    continue
                if not any(self.inside_rect(cell, r) for r in current):
                    continue
                dist = abs(cell[0] - cx) + abs(cell[1] - cy)
                if best is None or dist < best[0]:
                    best = (dist, name)
            if best is not None:
                return {"action": "move", "direction": best[1]}
            return {"action": "wait"}
        cells = set()
        for x0, y0, x1, y1, _ in rects:
            for x in range(max(0, min(x0, x1)), min(self.width - 1, max(x0, x1)) + 1):
                for y in range(max(0, min(y0, y1)), min(self.height - 1, max(y0, y1)) + 1):
                    if self.passable((x, y)):
                        cells.add((x, y))
        return self.next_move(self.bfs_to_any(here, cells))

    def active_heal_points(self, tick):
        return [
            p
            for p in self.heal
            if p[2] >= tick
            and self.in_bounds(p[0], p[1])
            # 模型有时把伤害事件坐标误写成 heal，落在生效中的 avoid 区内一律不信
            and not any(
                r[4] >= tick and self.inside_rect((p[0], p[1]), r)
                for r in self.avoid
            )
        ]

    def heal_action(self, tick, pre=False):
        # pre=True：满血也提前去点位蹲守，方便受伤后立刻进点回血
        if self.hp > 80 and not pre:
            return None
        points = self.active_heal_points(tick)
        if not points:
            return None
        here = self.me
        for x, y, _ in points:
            if here != (x, y):
                continue
            # 只有成功移动进入点位才会获得或刷新回血，站在点上无效，所以离点再进
            for name, (dx, dy) in DIRS.items():
                cell = (here[0] + dx, here[1] + dy)
                if self.passable(cell):
                    return {"action": "move", "direction": name}
            return {"action": "wait"}
        best = None
        for x, y, until in points:
            path = self.bfs_to_any(here, {(x, y)})
            if path is None or len(path) < 2:
                continue
            steps = len(path) - 1
            if steps > until - tick:
                continue
            if best is None or steps < best[0]:
                best = (steps, path)
        if best is None:
            return None
        return self.step_action(best[1])

    def unfinished_missions(self):
        return any(
            str(mission.get("id")) not in self.completed_ids
            for mission in self.missions
        )

    def unsafe_cell(self, cell, skip=None):
        return self.threat_count(cell, skip) > 0

    def hunt_line_cell(self, cell):
        # 打人只能在目标的正交 1~2 格，狩猎时允许进入目标的火线；
        # 但同时落在别的敌人火线里就不去，避免被夹击
        for (ex, ey), ent in self.enemies.items():
            if ent["protected"] or ent["defending"]:
                continue
            in_line = (ex == cell[0] and abs(ey - cell[1]) <= 2) or (
                ey == cell[1] and abs(ex - cell[0]) <= 2
            )
            if in_line and not self.unsafe_cell(cell, skip=(ex, ey)):
                return True
        return False

    def press_line_cell(self, cell):
        # 压制防御目标：站进它的正交 1~2 格才能打到它；它当 Tick 不还手，
        # 所以这条火线只有在它取消防御时才有威胁，收益对得起风险
        for (ex, ey), ent in self.enemies.items():
            if ent["protected"] or not ent["defending"]:
                continue
            in_line = (ex == cell[0] and abs(ey - cell[1]) <= 2) or (
                ey == cell[1] and abs(ex - cell[0]) <= 2
            )
            if in_line and not self.unsafe_cell(cell, skip=(ex, ey)):
                return True
        return False

    def valuable_cell(self, cell):
        tick = self.last_tick or 0
        if cell in self.resources:
            return True
        if self.unfinished_missions() and (
            cell in self.goto or cell == self.reach_mission()
        ):
            return True
        if any(self.inside_rect(cell, r) for r in self.stay if r[4] >= tick):
            return True
        return any(until >= tick for x, y, until in self.heal if (x, y) == cell)

    def contested_resource(self):
        # 1v1 抢同一个资源：视野里只有一辆敌车，且某个资源点我们比它更近
        # （或同时到）、又在几步之内。这时以抢资源为首——踩到就是确定的
        # R2 加分，而换一枪只是 20 点有效伤害
        if len(self.enemies) != 1 or self.me is None:
            return None
        (ex, ey) = next(iter(self.enemies))
        best = None
        for cell in self.resources:
            if not self.passable(cell):
                continue
            mine = abs(cell[0] - self.me[0]) + abs(cell[1] - self.me[1])
            theirs = abs(cell[0] - ex) + abs(cell[1] - ey)
            if mine > CONTEST_RANGE or theirs < mine:
                continue
            if best is None or mine < best[0]:
                best = (mine, cell)
        return None if best is None else best[1]

    def short_goals(self):
        goals = {c for c in self.resources if self.passable(c)}
        reach = self.reach_mission()
        if reach is not None and self.passable(reach):
            goals.add(reach)
        goals.update(c for c in self.goto if self.passable(c))
        return goals

    def bfs_distance(self, start, goals):
        if not goals:
            return None
        if start in goals:
            return 0
        queue = deque([start])
        dist = {start: 0}
        while queue:
            cur = queue.popleft()
            step = dist[cur] + 1
            for dx, dy in DIRS.values():
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in dist or not self.passable(nxt):
                    continue
                if nxt in goals:
                    return step
                dist[nxt] = step
                queue.append(nxt)
        return None

    def avoid_backtrack(self, action):
        # 两个目标互相拉扯时会出现来回一格震荡；但折返格经常就在去目标的最短路上，
        # 一票否决会把 bot 一路挤向反方向（实测会横穿全图丢掉第 4 个资源），
        # 所以只否决“更差的折返”：比较走折返格与绕行后的剩余路程
        if action.get("action") != "move" or self.me is None:
            return action
        # 脱离敌人火线的动作优先，不能被折返过滤改掉
        if self.urgent:
            return action
        if self.prev_pos is None or self.me in self.avoid_cells:
            return action
        dx, dy = DIRS[action["direction"]]
        cell = (self.me[0] + dx, self.me[1] + dy)
        if cell != self.prev_pos or self.valuable_cell(cell):
            return action

        goals = self.short_goals()
        forward = self.bfs_distance(cell, goals) if self.passable(cell) else None
        best = None
        for name, (ddx, ddy) in DIRS.items():
            alt = (self.me[0] + ddx, self.me[1] + ddy)
            if alt == self.prev_pos or not self.passable(alt):
                continue
            if self.unsafe_cell(alt):
                continue
            if self.valuable_cell(alt):
                return {"action": "move", "direction": name}
            dist = self.bfs_distance(alt, goals)
            score = dist if dist is not None else 10 ** 6
            if best is None or score < best[0]:
                best = (score, {"action": "move", "direction": name})
        if forward is not None and (best is None or forward <= best[0]):
            return action
        return best[1] if best is not None else {"action": "wait"}

    def kill_target(self):
        """正交 1~2 格内值得开火的目标方向（跳过保护目标）。

        一击即死随时打（防御目标每次只吃 10，按 10 判）；己方血量健康时
        两击可杀（敌 ≤40 HP）也算有把握。
        """
        for step, name, cell, ent in sorted(
            self.attack_candidates(), key=lambda c: c[3]["hp"]
        ):
            if ent["protected"]:
                continue
            dmg = 10 if ent["defending"] else 20
            if ent["hp"] <= dmg:
                return {"action": "attack", "direction": name}
            if not ent["defending"] and ent["hp"] <= 40 and self.hp > 40:
                return {"action": "attack", "direction": name}
        return None

    def combat_action(self):
        if self.nearest_enemy_in_line() is None:
            return None

        # 有效伤害已到本场停火线 760：遇到敌人先脱离
        if self.combat_done:
            self.urgent = True
            return self.escape_action() or {"action": "defend"}

        if self.protected:
            return None

        # 一击即死或残血目标随时打
        action = self.kill_target()
        if action is not None:
            return action

        tick = self.last_tick or 0
        usable = [
            cand
            for cand in self.attack_candidates()
            if not cand[3]["protected"] and not cand[3]["defending"]
        ]
        usable.sort(key=lambda c: (c[3]["hp"], c[0]))

        # 天下无双期间攻击收益高，视为值得开战
        if 0 < self.attack_all_until and tick <= self.attack_all_until and usable:
            return {"action": "attack", "direction": usable[0][1]}

        # 上一帧在防御的目标本 Tick 大概率不还手（防御即不攻击），这是白拿的伤害；
        # 只在 60 HP 以上、且没有第二个敌人夹击时打，避免它取消防御时被反打。
        # 例外：残血防御目标（≤20 HP）两枪就能补掉，低血也照打——收益 80 分，
        # 最坏也只挨一次反打
        if self.threat_count(self.me) <= 1:
            pokes = [
                cand
                for cand in self.attack_candidates()
                if not cand[3]["protected"]
                and cand[3]["defending"]
                and (self.hp > 60 or cand[3]["hp"] <= 20)
            ]
            if pokes:
                pokes.sort(key=lambda c: (c[3]["hp"], c[0]))
                return {"action": "attack", "direction": pokes[0][1]}

        # 按换血胜算决定：能在自己倒下前击败对方就打完。
        # 多敌集火时按合计火力计算，单挑能赢不代表二打一能赢。
        incoming = 20 * max(1, self.threat_count(self.me))
        my_hits = max(1, (self.hp + incoming - 1) // incoming)
        for step, name, cell, ent in usable:
            their_hits = max(1, (ent["hp"] + 19) // 20)
            if their_hits <= my_hits:
                return {"action": "attack", "direction": name}

        # 这一 Tick 的伤害按移动前位置结算，跑不跑都一样挨，先处理必死局面：
        # 防御能把伤害减半救回来就先保命，救不回来就把这一击打出去。
        # 尾盘 D<700 时这一击一定要打出去：多活 10 HP 也换不来一枪
        if self.hp <= incoming:
            if self.hp > incoming // 2 and not self.fight_to_death():
                return {"action": "defend"}
            if usable:
                return {"action": "attack", "direction": usable[0][1]}
            return {"action": "defend"}

        # 换血亏：找多步脱险路线。只走一步常被另一个敌人的火线接住，
        # 十字夹击里一两步内就有更安全的格子，实在没有才原地防御减半。
        self.urgent = True
        escape = self.escape_action()
        if escape is not None:
            return escape
        if usable:
            return {"action": "attack", "direction": usable[0][1]}
        return {"action": "defend"}

    def decide(self, p):
        me = p.get("self") or {}
        self.urgent = False
        if not me.get("alive", True):
            self.bastion_ticks = 0
            return {"action": "wait"}
        if self.me is None:
            return {"action": "wait"}

        here = self.me
        self.resources.pop(here, None)

        # 已经在告警区域内：先走到最近的区域外格子，不能原地等到事件结算
        if here in self.avoid_cells:
            safe = {
                (x, y)
                for x in range(self.width)
                for y in range(self.height)
                if (x, y) not in self.avoid_cells and self.passable((x, y))
            }
            action = self.next_move(self.bfs_to_any(here, safe))
            if action is not None:
                return action

        tick = self.last_tick or 0
        aoe_active = 0 < self.attack_all_until and tick <= self.attack_all_until

        # 低血先回血，死一次扣 60 分；范围攻击期间不中断抢攻窗口
        if self.hp <= 60 and not aoe_active:
            action = self.heal_action(tick)
            if action is not None:
                return action

        # 堡垒驻留要先攒满 10 Tick 才拿到无双增益，窗口本身不等于增益已生效；
        # 攒满之前专心守点，攒满之后才出去找敌人，否则整个窗口都在用普攻
        if any(self.inside_rect(here, r) for r in self.stay if r[4] >= tick):
            self.bastion_ticks += 1
        else:
            self.bastion_ticks = 0
        if self.stay and (
            self.bastion_ticks < 12 or not aoe_active or not self.enemies
        ):
            action = self.stay_action(tick)
            if action is not None:
                return action

        # 只剩 reach 未完成且血量够：踩点是确定的 5 分，继续对拼最多再挣 2 分，
        # 提前过去，别拖到尾盘被追着打而错过（踩到即算完成，不需要停留）
        if tick >= 320 and not aoe_active and self.hp > 50:
            reach = self.reach_mission()
            pending = [
                mission
                for mission in self.missions
                if str(mission.get("id")) not in self.completed_ids
            ]
            if reach is not None and len(pending) == 1 and reach != here:
                action = self.next_move(self.bfs_to_any(here, {reach}))
                if action is not None:
                    return action

        # 1v1 抢同一个资源：以抢资源为首。资源是确定的 R2 加分，而这时候
        # 开一枪只有 20 点有效伤害；只有一辆敌车时也不至于被夹击
        if (
            not aoe_active
            and self.hp > 60
            and self.score_resource < RESOURCE_TARGET
        ):
            cell = self.contested_resource()
            if cell is not None:
                action = self.next_move(self.bfs_to_any(here, {cell}))
                if action is not None:
                    return action

        # 前期抢资源：R2 每多 10 分资源项多 2.5 分，开局值得到处捡，
        # 但敌车已进入打击范围、或状态不佳时仍交给后面的战斗/回血逻辑
        if (
            not aoe_active
            and self.hp > 80
            and tick <= SPRINT_TICKS
            and self.score_resource < RESOURCE_TARGET
            and not self.attack_candidates()
        ):
            goals = {c for c in self.resources if self.passable(c)}
            action = self.next_move(self.bfs_to_any(here, goals))
            if action is not None:
                return action
            # 视野内没有资源就主动找：4 车分同一张图，原地追人只会把资源让出去
            if not self.enemies:
                action = self.exit_center_action(here, tick)
                if action is not None:
                    return action
                center = (self.width // 2, self.height // 2)
                goals = {c for c in self.goto if self.passable(c)} | self.center_ward(
                    self.frontier(), here
                )
                if self.passable(center):
                    goals.add(center)
                action = self.next_move(self.bfs_to_any(here, goals))
                if action is not None:
                    return action

        if self.missions and self.decision_type != "survive":
            if aoe_active:
                action = self.aoe_action(tick)
                if action is not None:
                    return action
                action = self.hunt_enemies()
                if action is not None:
                    return action
            action = self.heal_action(tick)
            if action is not None:
                return action
            action = self.combat_action()
            if action is not None:
                return action

        # 任务已完成且当前没有交战：满血也提前去天使点蹲守，受伤后立刻能进点回血
        # 视野里有资源时先收资源，别为提前蹲点丢掉分数
        if (
            not aoe_active
            and self.hp > 80
            and not self.resources
            and not self.unfinished_missions()
        ):
            action = self.heal_action(tick, pre=True)
            if action is not None:
                return action

        # 20 HP 以下又没有可用回血点：任务已完成后继续全图游走只会被路过的
        # 敌人一击带走（死一次 −60），就地防御/等待保住已积累的伤害分。
        # 尾盘 700 还没到手时不守：站住的代价是整条命都换不到伤害
        if (
            not aoe_active
            and self.hp <= 20
            and not self.fight_to_death()
            and not self.unfinished_missions()
            and not self.active_heal_points(tick)
        ):
            if self.enemies:
                return {"action": "defend"}
            return {"action": "wait"}

        target = self.reach_mission()

        if target is not None and target != here and (
            tick >= 380 or self.score_resource >= 120
        ):
            action = self.next_move(self.bfs_to_any(here, {target}))
            if action is not None:
                return action

        # 任务未完成时资源优先；任务完成后只顺手拿 8 格内的，远处的交给狩猎，
        # 别为了记忆里一个远期资源点把伤害窗口全走掉。前期冲刺阶段不设限：
        # 4 个陪练同时抢，远处的资源不先扑过去就被别人拿了
        goals = set(self.resources)
        sprint = tick <= SPRINT_TICKS and self.score_resource < RESOURCE_TARGET
        wanted = self.wanted_resource_types()
        if wanted and not sprint:
            narrowed = {c for c in goals if self.resources[c]["type"] in wanted}
            if narrowed:
                goals = narrowed
        if not self.unfinished_missions() and not sprint:
            goals = {
                c
                for c in goals
                if abs(c[0] - here[0]) + abs(c[1] - here[1]) <= 8
            }
        action = self.next_move(self.bfs_to_any(here, goals))
        if action is not None:
            return action

        # 有可见目标就先贴上去打，别被 goto/探索点拉走
        if (
            not aoe_active
            and self.decision_type != "survive"
            and self.enemies
            and self.should_hunt()
        ):
            action = self.hunt_enemies()
            if action is not None:
                return action

        # 40~60 HP 打不动眼前目标（残血敌在防御、健康敌不在射程）时，
        # 别原地绕圈，先占好下一枪的位置
        if (
            not aoe_active
            and self.decision_type != "survive"
            and self.enemies
        ):
            action = self.pressure_move()
            if action is not None:
                return action

        # 视野里没有敌人：朝最后看到的位置找，增加接战次数
        if (
            not aoe_active
            and self.decision_type != "survive"
            and not self.enemies
            and not self.unfinished_missions()
        ):
            action = self.hunt_memory()
            if action is not None:
                return action

        if target is not None and target != here:
            action = self.next_move(self.bfs_to_any(here, {target}))
            if action is not None:
                return action

        if self.unfinished_missions():
            goals = {cell for cell in self.goto if self.passable(cell)}
            action = self.next_move(self.bfs_to_any(here, goals))
            if action is not None:
                return action

        # 视野和记忆里都没有敌人：往地图中部靠。四个出生角只有中心区会反复撞上，
        # 沿边缘探索会一逛逛上百 Tick（实测满血空转 130 Tick，对手在中间打了 1000+）；
        # 平局保险到手后不再往人堆里扎，宁可沿边缘捡资源等事件
        if (
            not aoe_active
            and self.decision_type != "survive"
            and not self.enemies
            and not self.damage_locked()
            and tick > SPRINT_TICKS
        ):
            action = self.exit_center_action(here, tick)
            if action is not None:
                return action
            center = (self.width // 2, self.height // 2)
            action = self.next_move(self.bfs_to_any(here, {center}))
            if action is not None:
                return action

        action = self.next_move(
            self.bfs_to_any(here, self.center_ward(self.frontier(), here))
        )
        if action is not None:
            return action
        # 兜底避障：所有目标都不可达（被障碍/临时占位围住）时，原来只会原地
        # wait，表现为顶着墙不动；改成先挪到周边最空旷的可通行格再重新规划
        return self.detour_action() or {"action": "wait"}

    def passable(self, cell):
        if not self.in_bounds(*cell):
            return False
        if cell in self.blocked or cell in self.enemies:
            return False
        if self.blocked_until.get(cell, -1) >= (self.last_tick or 0):
            return False
        if cell in self.avoid_cells and self.me not in self.avoid_cells:
            return False
        # 低血时不再自己走进敌人 1~2 格正交火线，否则会被先手打死；
        # 主动狩猎时只对“当前目标”的火线放行，脱离/赶路仍保持原规则。
        # 平局保险到手后提前到 60 HP 就绕开火线：那一分已经拿到，死一次 -60
        if self.hp <= (60 if self.damage_locked() else 50) and self.unsafe_cell(cell):
            if not (
                (self.hunting and self.hunt_line_cell(cell))
                or (self.pressing and self.press_line_cell(cell))
            ):
                return False
        return True

    def exit_center_action(self, here, tick):
        """中心附近既没资源也没敌人：竖着离开中心去找。

        触发后至少沿同一方向走 CENTER_EXIT_TICKS 个 Tick，否则刚离开中心
        又会被“往中部靠”的规则拉回来，变成在中心来回横跳。
        路上看到资源或敌人立刻退出该模式。
        """
        if self.resources or self.enemies:
            self.exit_until = 0
            return None
        cx, cy = self.width // 2, self.height // 2
        if tick >= self.exit_until:
            if abs(here[0] - cx) + abs(here[1] - cy) > CENTER_RADIUS:
                return None
            self.exit_dir = "down" if here[1] <= cy else "up"
            self.exit_until = tick + CENTER_EXIT_TICKS
            self.get_logger().info(
                f"中心无可捡目标 pos={here}，向{self.exit_dir}离开中心寻找"
            )
        edge_y = self.height - 1 if self.exit_dir == "down" else 0
        return self.next_move(self.bfs_to_any(here, {(here[0], edge_y)}))

    def detour_action(self):
        """目标全不可达时的脱困步：挑一个周边障碍最少的相邻可通行格。

        只在 BFS 到不了任何目标时用，所以不会影响正常寻路；相邻格之间
        按 search_dirs 的顺序取舍，保持横向前提。四周全堵死才返回 None。
        """
        if self.me is None:
            return None
        best = None
        names = {delta: name for name, delta in DIRS.items()}
        for dx, dy in self.search_dirs(self.me):
            name = names[(dx, dy)]
            cell = (self.me[0] + dx, self.me[1] + dy)
            if not self.passable(cell) or cell == self.prev_pos:
                continue
            crowd = 0
            for ddx, ddy in DIRS.values():
                nxt = (cell[0] + ddx, cell[1] + ddy)
                if not self.in_bounds(*nxt) or nxt in self.blocked:
                    crowd += 1
            if best is None or crowd < best[0]:
                best = (crowd, {"action": "move", "direction": name})
        return None if best is None else best[1]

    def center_ward(self, cells, start=None):
        """找资源/探索时优先朝地图中心推进。

        出生角出发时，等距的未知格全在边缘，纯最近未知格的 BFS 会沿边走，
        实测满血空转上百 Tick；这里先只在“离中心不比当前更远”的格子里挑，
        缩不进中心（被墙封住）时才放开全部候选。
        """
        start = self.me if start is None else start
        if not cells or start is None:
            return cells
        cx, cy = self.width // 2, self.height // 2
        mine = abs(start[0] - cx) + abs(start[1] - cy)
        inward = {c for c in cells if abs(c[0] - cx) + abs(c[1] - cy) <= mine}
        return inward or cells

    def frontier(self):
        cells = set()
        for cell in self.free:
            x, y = cell
            for dx, dy in DIRS.values():
                nxt = (x + dx, y + dy)
                if (
                    self.in_bounds(*nxt)
                    and nxt not in self.blocked
                    and nxt not in self.free
                    and nxt not in self.avoid_cells
                ):
                    cells.add(nxt)
        return cells

    def search_dirs(self, cell):
        """BFS 出队邻居的顺序：先横后竖，横竖都先朝地图中线。

        BFS 按发现顺序确定父节点，同长度的最短路径里因此会优先选择
        “第一步横着走、且朝着中心”的那条。只影响等长路径的取舍，
        不影响总步数，也不影响可达性。

        例外：出生点贴边时，开局到吃到第一个资源（最多 EDGE_TICKS 个 Tick）
        反过来先竖后横；资源一到手立刻恢复横向前进，边横移边继续收资源。
        """
        cx, cy = self.width // 2, self.height // 2
        vertical_first = (
            self.edge_open
            and self.first_resource_tick is None
            and (self.last_tick or 0) <= EDGE_TICKS
        )
        order = []
        for name, (dx, dy) in DIRS.items():
            horizontal = 0 if dx != 0 else 1
            if vertical_first:
                horizontal = 1 - horizontal
            toward = 0 if (cx - cell[0]) * dx + (cy - cell[1]) * dy > 0 else 1
            order.append((horizontal, toward, name, dx, dy))
        order.sort()
        return [(dx, dy) for _, _, _, dx, dy in order]

    def bfs_to_any(self, start, goals):
        if not goals:
            return None
        if start in goals:
            return [start]

        queue = deque([start])
        prev = {start: None}
        while queue:
            cur = queue.popleft()
            for dx, dy in self.search_dirs(cur):
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in prev:
                    continue
                if not self.passable(nxt):
                    continue
                prev[nxt] = cur
                if nxt in goals:
                    return self.reconstruct(prev, nxt)
                queue.append(nxt)
        return None

    def next_move(self, path):
        # 路径不足一步（已在目标上或不可达）就当没有动作，调用方继续往下挑目标
        if path is None or len(path) < 2:
            return None
        return self.step_action(path)

    @staticmethod
    def reconstruct(prev, end):
        path = []
        cur = end
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    @staticmethod
    def step_action(path):
        (x0, y0), (x1, y1) = path[0], path[1]
        delta = (x1 - x0, y1 - y0)
        for name, value in DIRS.items():
            if value == delta:
                return {"action": "move", "direction": name}
        return {"action": "wait"}


def main(args=None):
    rclpy.init(args=args)
    node = BotNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
