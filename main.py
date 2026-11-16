"""AstrBot 插件 — 自消息守卫（防自转）

有些平台会把机器人自己账号发出的消息也当作消息事件下发回来，
如果唤醒判定放行，机器人就会对着自己的话继续回，形成「自转」循环。

这个插件在事件进入 LLM 之前把它拦下来，但比平台总开关精细：

    • 支持三种模式：self（全拦）/ dedup（只拦与自己刚发出内容重复的）/ rate（只拦高频循环）
    • 支持跨账号回环检测：别的机器人引用或复读本机器人刚说的话时，静默本会话
    • 支持会话静默：本会话机器人短时间发言过多时，暂停后续自动回复
    • 支持放行前缀：以指定前缀开头的自己消息照常处理（例如手机端手敲的指令）
    • 支持会话白名单、群/私聊范围控制
    • 放行但又不该触发 LLM 的自己消息，会单独禁用默认 LLM 请求，插件指令不受影响

命令（需要管理员）：
    /自转守卫        查看当前配置与拦截统计
    /自转守卫日志    看最近 10 条拦截记录
    /自转守卫测试 xx 只判断不拦截，看这句话会不会被拦
    /自转守卫帮助    命令说明
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from difflib import SequenceMatcher
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_self_msg_guard"
SENT_MAXLEN = 500
LOG_MAXLEN = 50


# ---------------------------------------------------------------- 配置工具


def _cfg(config: Any, key: str, default: Any = None) -> Any:
    try:
        value = config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是", "开"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否", "关"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,\n;]", value) if p.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


_QUOTE_SHELL = re.compile(r"<Quoted Message>.*?</Quoted Message>", re.S)
_REMINDER_SHELL = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)


def _normalize(text: str) -> str:
    """剥掉引用/系统提醒外壳，再去掉空白与零宽字符，便于比对。"""
    text = text or ""
    text = _QUOTE_SHELL.sub("", text)
    text = _REMINDER_SHELL.sub("", text)
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    return re.sub(r"\s+", "", text).strip()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


_AT_PREFIX = re.compile(r"^\s*@[^@\s]{1,32}?\(\d{4,12}\)")
_AT_NAME = re.compile(r"^\s*@[^@\s(]{1,32}\s*")


def _strip_at(text: str) -> str:
    """抹掉开头的 @某某 / @某某(123456) 前缀。

    引用组件的 message_str 会带着 at 名与数字 ID，正文又常被重复一遍，
    两者叠加会把相似度稀释到阈值之下，所以比对前先做归一化。
    必须先匹配带 ID 的形式，否则非贪婪会只吃掉「@M」把名字切碎。
    """
    out = text or ""
    for _ in range(4):
        new = _AT_PREFIX.sub("", out, count=1)
        if new == out:
            new = _AT_NAME.sub("", out, count=1)
        if new == out:
            break
        out = new
    return out


def _dedup_texts(items: list[str]) -> list[str]:
    """去掉重复与被包含的片段，保留信息量最大的那份。"""
    out: list[str] = []
    for raw in items:
        norm = _normalize(_strip_at(raw))
        if not norm:
            continue
        if any(norm == o or norm in o for o in out):
            continue
        out = [o for o in out if o not in norm]
        out.append(norm)
    return out


def _longest_match(a: str, b: str) -> int:
    """最长公共子串长度，用来对抗前缀/重复带来的稀释。"""
    if not a or not b:
        return 0
    blocks = SequenceMatcher(None, a, b).get_matching_blocks()
    return max((blk.size for blk in blocks), default=0)


# ---------------------------------------------------------------- 插件本体


class SelfMsgGuardPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        self._sent: deque[tuple[float, str, str]] = deque(maxlen=SENT_MAXLEN)
        self._peer_ts: dict[tuple[str, str], deque[float]] = {}
        self._seen_self: dict[str, deque[float]] = {}
        self._out_ts: dict[str, deque[float]] = {}
        self._mute_log_at: dict[str, float] = {}
        self._records: deque[str] = deque(maxlen=LOG_MAXLEN)
        self._last_reply_raw: list[str] = []
        self._learned_bots: dict[str, float] = {}  # sender_id -> 过期时间戳
        self._loop_hits: dict[tuple[str, str], deque[float]] = {}
        self._quote_hits: dict[tuple[str, str], deque[float]] = {}   # 引用/@ 了机器人的轮次
        self._peer_last_msg: dict[tuple[str, str], float] = {}      # 对方最后一次说话的时间
        self._rt: dict[tuple[str, str], deque[float]] = {}          # 往返耗时（秒）
        self._throttle_at: dict[str, float] = {}
        self.blocked_total = 0
        self.passed_total = 0
        self.muted_total = 0

    # ------------------------------------------------ 配置读取

    @property
    def enabled(self) -> bool:
        return _as_bool(_cfg(self.config, "enabled", True), True)

    @property
    def mode(self) -> str:
        mode = str(_cfg(self.config, "mode", "self")).strip().lower()
        return mode if mode in {"self", "dedup", "rate"} else "self"

    @property
    def dedup_all_senders(self) -> bool:
        return _as_bool(_cfg(self.config, "dedup_all_senders", True), True)

    @property
    def loop_mute_only(self) -> bool:
        return _as_bool(_cfg(self.config, "loop_mute_only", True), True)

    @property
    def active_stop(self) -> bool:
        """命中判定时是否主动终止事件（而非只掐掉自动回复）。"""
        return _as_bool(_cfg(self.config, "active_stop", True), True)

    @property
    def flood_mute_all_senders(self) -> bool:
        return _as_bool(_cfg(self.config, "flood_mute_all_senders", False), False)

    @property
    def exempt_sender_ids(self) -> set[str]:
        """永不静默的发送者（真人好友填这里，避免误伤）。"""
        raw = _cfg(self.config, "exempt_sender_ids", []) or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,\s;]+", raw) if x]
        return {str(x).strip() for x in raw if str(x).strip()}

    @property
    def peer_bot_ids(self) -> set[str]:
        """对端机器人账号：来自这里或自动学到的才允许主动刹车。"""
        raw = _cfg(self.config, "peer_bot_ids", []) or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,\s;]+", raw) if x]
        return {str(x).strip() for x in raw if str(x).strip()}

    def _is_peer_bot(self, sender_id: str) -> bool:
        """只有确认是非人类对端机器人，才敢主动掐掉整条消息。"""
        if not sender_id or sender_id in self.exempt_sender_ids:
            return False
        return sender_id in self.peer_bot_ids or self._is_learned_bot(sender_id)

    @property
    def learned_bot_ttl(self) -> float:
        """学来的对端机器人多久后失效（秒，0 = 永不过期）。"""
        return max(0.0, _as_float(_cfg(self.config, "learned_bot_ttl_sec", 1800), 1800.0))

    @property
    def learned_bots_hard_stop(self) -> bool:
        """学来的对端机器人要不要主动刹车；默认只静音自动回复，避免误伤真人。"""
        return _as_bool(_cfg(self.config, "learned_bots_hard_stop", False), False)

    def _learn_bot(self, sender_id: str) -> None:
        """把某个账号学成对端机器人（带过期时间，误学不会永久生效）。"""
        if not sender_id:
            return
        self._learned_bots[sender_id] = time.time() + self.learned_bot_ttl

    def _is_learned_bot(self, sender_id: str) -> bool:
        exp = self._learned_bots.get(sender_id)
        if not exp:
            return False
        if exp <= time.time():
            self._learned_bots.pop(sender_id, None)
            return False
        return True

    def _brake(self, event: AstrMessageEvent, text: str, reason: str) -> None:
        """学来的对端机器人：默认只静音自动回复（不停事件），配置后才主动刹车。"""
        if self.learned_bots_hard_stop:
            self._hard_stop(event, text, reason)
        else:
            self._quiet(event, text, reason, stop=False)

    @property
    def block_llm(self) -> bool:
        return _as_bool(_cfg(self.config, "block_llm_for_self_msg", True), True)

    @property
    def pingpong_ignore_echo(self) -> bool:
        """群里"引用回话"的乒乓是否也算（不必正文复读）。见 2026-10-22 的注释。"""
        return _as_bool(_cfg(self.config, "pingpong_ignore_echo", False), False)

    @property
    def log_blocked(self) -> bool:
        return _as_bool(_cfg(self.config, "log_blocked", True), True)

    @property
    def verbose(self) -> bool:
        return _as_bool(_cfg(self.config, "verbose_log", False), False)

    @property
    def throttle_sec(self) -> float:
        """同一会话两次回应之间的最小间隔（秒），0 表示关闭节流。"""
        if not _as_bool(_cfg(self.config, "throttle_enabled", True), True):
            return 0.0
        return max(0.0, _as_float(_cfg(self.config, "throttle_window_sec", 5), 5.0))

    def _allow_prefixes(self) -> list[str]:
        return _as_list(_cfg(self.config, "allow_prefixes", ["!self"]))

    def _whitelist(self) -> list[str]:
        return _as_list(_cfg(self.config, "session_whitelist", []))

    # ------------------------------------------------ 内部判定

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        try:
            private = event.is_private_chat()
        except Exception:
            private = False
        if private and not _as_bool(_cfg(self.config, "apply_private", True), True):
            return False
        if not private and not _as_bool(_cfg(self.config, "apply_group", True), True):
            return False

        white = self._whitelist()
        if not white:
            return True
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        sender = str(event.get_sender_id() or "")
        group = str(event.get_group_id() or "") if hasattr(event, "get_group_id") else ""
        for item in white:
            if item and (item == origin or item == sender or (group and item == group)):
                return True
            if item and origin and item in origin:
                return True
        return False

    def _is_allowed_prefix(self, text: str) -> str:
        raw = (text or "").lstrip()
        for prefix in self._allow_prefixes():
            if prefix and raw.startswith(prefix):
                return prefix
        return ""

    def _remember(self, session: str, text: str) -> None:
        norm = _normalize(text)
        if len(norm) < 2:
            return
        self._sent.append((time.time(), session, norm))

    def _remember_peers(self, session: str, peers: set[str]) -> None:
        """记录机器人刚回给了谁，以及这次「对方说完 → 机器人回完」花了多久。

        ★ 2026-11-17：节律要看的是**往返耗时**（对方一句话、它 10~20 秒就回），
        而不是机器人自己两次发言的间隔——中间还夹着对方的发言和生成时间，
        按后者算会把正常的 20~40 秒回合一概排除掉。
        """
        if not peers:
            return
        now = time.time()
        for peer in peers:
            bucket = self._peer_ts.setdefault((session, peer), deque(maxlen=64))
            bucket.append(now)
            last = self._peer_last_msg.get((session, peer), 0.0)
            if last and 0 < now - last < 600:
                rt = self._rt.setdefault((session, peer), deque(maxlen=64))
                rt.append(now - last)

    def _note_peer_msg(self, session: str, sender_id: str) -> None:
        """记下「这个人在这个会话刚说了一句」，供下次机器人回完时算往返耗时。"""
        if not hasattr(self, "_peer_last_msg"):
            self._peer_last_msg = {}
        if session and sender_id:
            self._peer_last_msg[(session, sender_id)] = time.time()

    def _is_pingpong(self, session: str, sender_id: str) -> bool:
        """同一会话里，机器人与同一个人已来回多轮时判为互引乒乓。

        互引和自转不同：机器人先 @ 对方，对方再 @ 回来，双方引用的都不是
        机器人自己的原话，所以引用回环判据永远抓不到，只能靠轮次兜住。
        """
        rounds = _as_int(_cfg(self.config, "pingpong_rounds", 3), 3)
        if rounds <= 0 or not sender_id:
            return False
        window = max(1, _as_int(_cfg(self.config, "pingpong_window_sec", 120), 120))
        bucket = self._peer_ts.get((session, sender_id))
        if not bucket:
            return False
        now = time.time()
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) < rounds:
            return False
        # ★ 2026-11-17：再加一道节律条件。两台机器对刷时，对方几乎总在 10~20 秒内
        #   回过来；真人哪怕连着聊，也很难连续几轮都这么快。所以要求最近几轮的
        #   间隔中位数不超过 pingpong_gap_sec（默认 30 秒），才判成乒乓。
        gap_limit = _as_float(_cfg(self.config, "pingpong_gap_sec", 25), 25.0)
        if gap_limit <= 0:
            return True
        rt = list(self._rt.get((session, sender_id), ()))[-rounds:]
        if len(rt) < max(2, rounds - 1):
            return False          # 往返样本不够，先不判
        rt.sort()
        median = rt[len(rt) // 2]
        return median <= gap_limit

    def _is_loop_bot(self, session: str, sender_id: str) -> bool:
        """同一账号短时间内反复引用机器人原话，基本可以认定是对端机器人。

        真人偶尔引用一次很正常，所以要有阈值；跨过阈值就学进名单，
        之后它的引用回环才允许主动刹车。
        """
        rounds = _as_int(_cfg(self.config, "pingpong_rounds", 3), 3)
        if rounds <= 0 or not sender_id:
            return False
        window = max(1, _as_int(_cfg(self.config, "pingpong_window_sec", 120), 120))
        bucket = self._loop_hits.setdefault((session, sender_id), deque(maxlen=64))
        now = time.time()
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        bucket.append(now)
        return len(bucket) >= rounds

    def _is_quote_bot(self, session: str, sender_id: str) -> bool:
        """同一账号反复「引用/@ 机器人但说自己的话」，也算对端机器人。

        ★ 2026-11-14 补的口子：上面那条 `_is_loop_back` 分支一旦命中（引用的是
        机器人原话），四档判断里第 3 档要求「正文也在复读原话」。于是「每轮引用
        宁宁 + @ 宁宁 + 说自己的新内容」这种最典型的互引回环，永远落到第 4 档
        「疑似真人，放行」并 return，后面那段 pingpong 判据根本走不到。
        实测：模拟 6 轮、间隔 8 秒，默认配置和 pingpong_ignore_echo=true 都不停。

        现在单独计数：同一发送者在窗口内第 N 次「引用或 @ 机器人」就学成对端机器人，
        默认只静音自动回复（不 stop 事件）；learned_bots_hard_stop=true 时才真停。
        """
        rounds = _as_int(_cfg(self.config, "quote_loop_rounds", 5), 5)
        if rounds <= 0 or not sender_id:
            return False
        window = max(1, _as_int(_cfg(self.config, "pingpong_window_sec", 120), 120))
        if not hasattr(self, "_quote_hits"):     # 热重载留下的老实例没有这个字段
            self._quote_hits = {}
        bucket = self._quote_hits.setdefault((session, sender_id), deque(maxlen=64))
        now = time.time()
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        bucket.append(now)
        return len(bucket) >= rounds

    def _is_echo(self, session: str, norm: str) -> bool:
        window = max(1, _as_int(_cfg(self.config, "dedup_window_sec", 120), 120))
        min_len = max(1, _as_int(_cfg(self.config, "dedup_min_len", 4), 4))
        ratio = min(1.0, max(0.6, _as_float(_cfg(self.config, "dedup_ratio", 0.9), 0.9)))
        now = time.time()
        if len(norm) < min_len:
            return False
        for ts, sess, sent_norm in reversed(self._sent):
            if now - ts > window:
                break
            if sess != session:
                continue
            if len(sent_norm) < min_len:
                continue
            if _similar(norm, sent_norm) >= ratio:
                return True
        return False

    def _is_flood(self, session: str) -> bool:
        """只判断，不计数。

        ★ 2026-11-14：以前这里顺手 append，于是 `judge()` 带副作用——
        `/自转守卫测试` 连测几次就会把 `_seen_self` 填满，随后真实的自消息
        被误判成"高频自转"。计数挪到 `_record_self_hit()`，只在 _guard 里调。
        """
        limit = _as_int(_cfg(self.config, "rate_limit", 5), 5)
        if limit <= 0:
            return False
        window = max(1, _as_int(_cfg(self.config, "rate_window_sec", 60), 60))
        now = time.time()
        bucket = self._seen_self.get(session)
        if not bucket:
            return False
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        return len(bucket) >= limit

    def _record_self_hit(self, session: str) -> None:
        """记一次「自己账号在这个会话发了话」，供 _is_flood 统计。"""
        limit = _as_int(_cfg(self.config, "rate_limit", 5), 5)
        if limit <= 0:
            return
        bucket = self._seen_self.setdefault(session, deque(maxlen=limit + 5))
        bucket.append(time.time())

    def _record_out(self, session: str) -> None:
        limit = max(1, _as_int(_cfg(self.config, "rate_limit", 5), 5))
        window = max(1, _as_int(_cfg(self.config, "rate_window_sec", 60), 60))
        now = time.time()
        bucket = self._out_ts.setdefault(session, deque(maxlen=limit + 10))
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        bucket.append(now)

    def _is_flood_out(self, session: str) -> bool:
        """本会话中机器人近期发言是否已经过量（只判断，不计数）。"""
        limit = _as_int(_cfg(self.config, "rate_limit", 5), 5)
        if limit <= 0:
            return False
        window = max(1, _as_int(_cfg(self.config, "rate_window_sec", 60), 60))
        bucket = self._out_ts.get(session)
        if not bucket:
            return False
        now = time.time()
        return sum(1 for ts in bucket if now - ts <= window) >= limit

    def _debug_path(self) -> str:
        """调试文件的位置：data/plugin_data/astrbot_plugin_self_msg_guard/。

        审核要求持久化数据必须落在这个目录下；以前是往 data/ 根目录写，
        既不合规，内容里又带着会话文本和发送者 QQ。
        """
        try:
            from astrbot.core.star.star_tools import StarTools

            base = str(StarTools.get_data_dir("astrbot_plugin_self_msg_guard"))
        except Exception:
            here = os.path.dirname(os.path.abspath(__file__))
            base = os.path.join(os.path.dirname(os.path.dirname(here)),
                                "plugin_data", "astrbot_plugin_self_msg_guard")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "self_guard_debug.jsonl")

    def _debug(self, payload: dict) -> None:
        """把判定现场写成独立 jsonl，避免被主日志轮转吃掉。"""
        if not self.verbose:
            return
        try:
            path = self._debug_path()
            if os.path.exists(path) and os.path.getsize(path) > 262144:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("")
            payload["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            line = json.dumps(payload, ensure_ascii=False) + chr(10)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass

    @staticmethod
    def _chain_peers(chain: Any, self_id: str = "") -> set[str]:
        """从消息链里取出 At 与 Reply 指向的人（排除自己）。"""
        peers: set[str] = set()
        for comp in chain or []:
            name = type(comp).__name__
            target = ""
            if "At" in name:
                target = getattr(comp, "qq", None) or getattr(comp, "target", None) or ""
            elif "Reply" in name:
                target = getattr(comp, "sender_id", None) or ""
            target = str(target or "")
            if target and target != self_id:
                peers.add(target)
        return peers

    def _extract_reply(self, event: AstrMessageEvent) -> tuple[str, bool]:
        """取消息链里 Reply 组件的被引用内容。

        被引用文本不会出现在 event.message_str 中，只存在于 Reply 组件里，
        所以回环判定必须直接读 event.message_obj.message。
        返回（被引用纯文本, 是否引用了本机器人自己）。
        """
        parts: list[str] = []
        quoted_self = False
        raw: list[str] = []
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        try:
            chain = event.message_obj.message or []
        except Exception:
            chain = []
        for comp in chain:
            try:
                name = type(comp).__name__
                if "Reply" not in name:
                    continue
                sender_id = str(getattr(comp, "sender_id", "") or "")
                raw.append(
                    "Reply(sender_id=%s, message_str=%r, chain=%r)"
                    % (
                        sender_id,
                        str(getattr(comp, "message_str", "") or "")[:80],
                        [
                            "%s:%s" % (type(i).__name__, str(getattr(i, "text", "") or "")[:40])
                            for i in (getattr(comp, "chain", None) or [])
                        ][:5],
                    )
                )
                if sender_id and self_id and sender_id == self_id:
                    quoted_self = True
                sub = getattr(comp, "message_str", "") or ""
                if sub:
                    parts.append(str(sub))
                for item in getattr(comp, "chain", None) or []:
                    for name in ("text", "message_str", "url", "file"):
                        value = getattr(item, name, None)
                        if isinstance(value, str) and value:
                            parts.append(value)
                            break
            except Exception:
                continue
        self._last_reply_raw = raw
        return "".join(_dedup_texts(parts)), quoted_self

    def _is_loop_back(self, session: str, norm: str, reply_norm: str = "") -> bool:
        """跨账号回环判定：别人（常是别的机器人）复读/引用机器人刚说过的内容。

        被引用文本与对方自己的话要分别比对：把两者拼成一坨再算相似度，会被
        对方多出来的语句稀释，导致「引用机器人原话」这种最典型的情况反而漏判。
        """
        window = max(1, _as_int(_cfg(self.config, "dedup_window_sec", 120), 120))
        min_len = max(2, _as_int(_cfg(self.config, "loop_min_len", 6), 6))
        ratio = min(1.0, max(0.3, _as_float(_cfg(self.config, "loop_ratio", 0.55), 0.55)))
        candidates = [c for c in (reply_norm, norm) if c and len(c) >= min_len]
        if not candidates:
            return False
        now = time.time()
        for ts, sess, sent_norm in reversed(self._sent):
            if now - ts > window:
                break
            if sess != session or len(sent_norm) < min_len:
                continue
            for cand in candidates:
                # 引用原文常与本机发言只差一个「@某某」前缀，所以用双向包含判定
                if cand in sent_norm or sent_norm in cand:
                    return True
                if _similar(cand, sent_norm) >= ratio:
                    return True
                # 正文被重复或夹了别的话时，靠最长公共子串兜底
                if _longest_match(cand, sent_norm) >= max(min_len, int(len(sent_norm) * 0.8)):
                    return True
        return False

    def judge(self, session: str, text: str) -> tuple[bool, str]:
        """返回 (是否拦截, 原因)。不产生副作用以外的统计。"""
        mode = self.mode
        norm = _normalize(text)
        if mode == "self":
            return True, "self 模式：本账号消息一律不进入对话"
        if mode == "dedup" and self._is_echo(session, norm):
            return True, "与机器人近期发出的内容重复"
        if self._is_flood(session):
            return True, "短时间高频自己消息，判定为自转循环"
        return False, ""

    def _add_record(self, event: AstrMessageEvent, text: str, reason: str) -> None:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime())
        session = str(getattr(event, "unified_msg_origin", "") or "?")
        self._records.append(f"{stamp} [{session[-24:]}] {reason} :: {text[:40]}")

    def _mute(self, event: AstrMessageEvent, text: str, reason: str) -> None:
        """静默本会话：默认只掐掉自动回复，active_stop 时改为主动刹车。"""
        self._quiet(event, text, reason, stop=self.active_stop)

    def _hard_stop(self, event: AstrMessageEvent, text: str, reason: str) -> None:
        """主动刹车：既掐掉自动回复，也终止事件继续往下走。"""
        self._quiet(event, text, reason, stop=True)

    def _quiet(self, event, text: str, reason: str, stop: bool) -> None:
        # ★ 2026-09-27 修正：should_call_llm(True) 才是「禁止默认 LLM」
        #   （pipeline: `if not event.call_llm: 调用 LLM`）；原来传 False = 反而放行

        self.muted_total += 1
        session = str(getattr(event, "unified_msg_origin", "") or "")
        tag = "主动停止" if stop else "静默本会话"
        if self.log_blocked:
            last = self._mute_log_at.get(session, 0.0)
            if time.time() - last > 10:
                self._mute_log_at[session] = time.time()
                logger.info(f"[self_guard] {tag}：{reason} :: {text[:60]}")
        self._add_record(event, text, reason)
        if stop:
            event.should_call_llm(True)
            event.stop_event()
        elif self.loop_mute_only:
            event.should_call_llm(True)
        else:
            event.stop_event()

    # ------------------------------------------------ 事件钩子

    @filter.on_decorating_result(priority=100)
    async def _record_sent(self, event: AstrMessageEvent):
        """记录机器人即将发出去的内容与时间，供回环/静默判定使用。"""
        if not self.enabled:
            return
        session = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            result = event.get_result()
            text = result.get_plain_text() if result else ""
        except Exception:
            text = ""
        text = (text or "").strip()
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        try:
            chain = event.message_obj.message or []
        except Exception:
            chain = []
        self._remember_peers(session, self._chain_peers(chain, self_id))
        if not text:
            return
        self._remember(session, text)
        self._record_out(session)

    # ★ 2026-09-27：平台会把「动作类」事件也回灌进来（如 `offline_file`：对方下载了离线文件），
    #   私聊里它们同样会被判为「已唤醒」→ 空内容也会触发一次默认 LLM 回复（用户报的
    #   「她传文件给我、我下载之后她还回一次」就是这个）。这里只**禁止默认 LLM**，
    #   不 stop_event —— 免得把 pokepro 这类「自己调 LLM」的插件一起掐死。
    NON_MESSAGE_ALLOW_NOTICE = ("poke",)      # 戳一戳要放行（她要能戳回去）

    def _is_spurious_event(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """返回 (是否不该触发默认 LLM, 原因)。"""
        raw = None
        try:
            raw = getattr(event.message_obj, "raw_message", None)
        except Exception:
            raw = None
        post = notice = ""
        if isinstance(raw, dict):
            post = str(raw.get("post_type") or "")
            notice = str(raw.get("notice_type") or "")
        if post and post != "message":
            if any(k in notice for k in self.NON_MESSAGE_ALLOW_NOTICE):
                return False, ""
            return True, "非消息事件（post_type=%s, notice_type=%s）" % (post, notice)
        try:
            chain = list(event.message_obj.message or [])
        except Exception:
            chain = []
        kinds = [type(c).__name__ for c in chain]
        if not (event.message_str or "").strip() and not kinds:
            return True, "空内容事件（既无文字也无任何消息组件）"
        return False, ""

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def _guard(self, event: AstrMessageEvent):
        """在其它处理器与 LLM 之前，拦下机器人自己发出的消息。"""
        if not self.enabled:
            return
        if _as_bool(_cfg(self.config, "block_spurious_events", True), True):
            spurious, why = self._is_spurious_event(event)
            if spurious:
                self.spurious_total = getattr(self, "spurious_total", 0) + 1
                event.should_call_llm(True)          # 只掐默认 LLM
                if self.log_blocked and self.spurious_total % 10 == 1:
                    logger.info("[self_guard] 非消息/空内容事件，已禁用默认 LLM：%s" % why)
                self._add_record(event, "", why)
                return
        self_id = str(event.get_self_id() or "")
        sender_id = str(event.get_sender_id() or "")
        session = str(getattr(event, "unified_msg_origin", "") or "")
        text = event.message_str or ""

        # ---- 不是本账号发的：只做跨账号回环 / 会话静默检测，绝不误伤人类
        if not self_id or self_id != sender_id:
            # 先过作用域与豁免名单，再谈节流：豁免的人和不在作用域的会话
            # 不该被节流牵连，更不该替别人背一次「本次先停」。
            if not self._in_scope(event) or sender_id in self.exempt_sender_ids:
                return

            # ---- 节流：同一个人在同一个会话里最小间隔内只放行一次请求，防止连珠炮式刷屏
            # 按「会话 + 发送者」计数：只压同一个人在同一个地方连发，
            # 别人正常说话不会被连带掐掉，也不会跨群牵连同一个人。
            window = self.throttle_sec
            throttle_key = f"{session}|{sender_id}"
            if session and window > 0:
                now = time.time()
                # 顺手清掉过期记录，避免字典随会话数量无限增长
                if len(self._throttle_at) > 64:
                    expire = now - max(window, 60.0)
                    self._throttle_at = {
                        k: v for k, v in self._throttle_at.items() if v > expire
                    }
                last = self._throttle_at.get(throttle_key, 0.0)
                if now - last < window:
                    self._hard_stop(
                        event, text, f"该发送者 {window:g} 秒内已有一次回应，本次先停"
                    )
                    return
                self._throttle_at[throttle_key] = now
            self._note_peer_msg(session, sender_id)
            norm = _normalize(text)
            reply_norm, quoted_self = self._extract_reply(event)
            if self.verbose:
                logger.info(
                    "[self_guard][probe] sender=%s quoted_self=%s reply=%r text=%r sent=%d"
                    % (sender_id, quoted_self, reply_norm[:60], norm[:60], len(self._sent))
                )
                self._debug(
                    {
                        "kind": "probe",
                        "session": session,
                        "sender": sender_id,
                        "self": self_id,
                        "text": norm[:120],
                        "reply": reply_norm[:120],
                        "quoted_self": quoted_self,
                        "pingpong": self._is_pingpong(session, sender_id),
                        "reply_raw": getattr(self, "_last_reply_raw", []),
                        "sent_recent": [
                            t[:80] for _, sess, t in list(self._sent)[-3:] if sess == session
                        ],
                    }
                )
            # 正文（不是引用）是否在复读机器人原话 —— 真机器人会复读，真人只会引用
            echo_only = bool(norm) and self._is_loop_back(session, norm, "")
            if self.dedup_all_senders and (norm or reply_norm) and self._is_loop_back(
                session, norm, reply_norm
            ):
                reason = (
                    "引用了机器人自己刚发出的内容，疑似跨账号回环"
                    if reply_norm
                    else "复读/接话命中了机器人刚发出的内容，疑似跨账号回环"
                )
                # 真人引用机器人消息太正常了（聊天时经常引用来接话），所以分三档：
                # 1) peer_bot_ids 里写明的对端机器人 → 主动刹车；
                # 2) 学来的对端机器人 → 默认只静音自动回复，配置后才会刹车；
                # 3) 「学」要求正文本身在复读机器人原话，光引用不算。
                if reply_norm and sender_id in self.peer_bot_ids:
                    self._hard_stop(event, text or reply_norm, reason)
                elif self._is_learned_bot(sender_id):
                    # 不看有没有引用：学到的对端机器人，无论是引用还是跟着复读同一句，
                    # 都继续静音（原来只在 reply_norm 时判，复读同一句口头禅就漏掉了）
                    self._brake(event, text or reply_norm, reason + "（学到的对端机器人）")
                elif reply_norm and echo_only and self._is_loop_bot(session, sender_id):
                    self._learn_bot(sender_id)
                    self._brake(
                        event,
                        text or reply_norm,
                        reason + "（正文在复读机器人原话，学为对端机器人）",
                    )
                elif self._is_quote_bot(session, sender_id):
                    # 反复「引用/@ 机器人 + 说自己的新内容」：不是复读，但明显是互引回环
                    self._learn_bot(sender_id)
                    self._brake(
                        event,
                        text or reply_norm,
                        reason + "（反复引用/@，学为对端机器人）",
                    )
                else:
                    self._add_record(event, text or reply_norm, reason + "（疑似真人，放行）")
                    if self.verbose:
                        logger.info(
                            "[self_guard] 放行（疑似真人引用机器人消息）：%s" % (text or reply_norm)[:40]
                        )
                return
            mentions_self = quoted_self
            if not mentions_self:
                try:
                    for comp in event.message_obj.message or []:
                        if "At" not in type(comp).__name__:
                            continue
                        value = str(
                            getattr(comp, "qq", None) or getattr(comp, "target", None) or ""
                        )
                        if value and value == self_id:
                            mentions_self = True
                            break
                except Exception:
                    pass
            # ★ 2026-10-22：**群里"引用回话"的乒乓也算循环**，不再要求正文复读。
            #   实况（群 748791823）：小杏仁 每条都在 `[Quote(绫地宁宁…)]`，
            #   宁宁回 → 她又引用 → 宁宁又回，内容每次都不重复 → `echo_only=False`
            #   → 旧判据永远不刹车，只能靠人发 `/stop`。
            #   口径：**仅群聊**、且 `pingpong_ignore_echo=true` 时，只要「机器人 @ 过对方 N 轮
            #   ＋ 对方这次又在引用/@ 机器人」就刹车；私聊不放（正常聊天就是来回）。
            #   ⚠️ 2026-10-23 修：这里原来写了 `_is_private_chat(event)`——那是 dsh_relay 里的函数，
            #   本插件没有，直接 NameError 把整个 _guard 打挂（日志里 12:53:46 抓到）。用 event 自己的方法。
            try:
                _pp_private = bool(event.is_private_chat())
            except Exception:
                _pp_private = False
            _pp_ignore_echo = bool(self.pingpong_ignore_echo) and not _pp_private
            # ★ 2026-11-17：已经被判定/学成对端机器人的号，只要还在 @/引用机器人，
            #   就继续静音，不必每次都重新攒够轮次（原来攒不够就放行，表现为忽停忽不停）。
            if mentions_self and self._is_learned_bot(sender_id):
                self._brake(event, text or reply_norm, "已学为对端机器人，继续静音")
                return
            if (
                self.dedup_all_senders
                and mentions_self
                and (echo_only or _pp_ignore_echo)
                and self._is_pingpong(session, sender_id)
            ):
                # 来回多轮、正文还在复读机器人原话，才当对端机器人；真人 @ 回来不算。
                # pingpong 的计数是「机器人自己 @ 过对方」喂出来的，光凭这一点就把人
                # 学成机器人，会把正常聊天的人永久静音（2026-10-20 实测踩过）。
                if sender_id not in self.peer_bot_ids:
                    self._learn_bot(sender_id)
                self._brake(
                    event,
                    text or reply_norm,
                    "与同一个人来回多轮、且正文在复读机器人原话，先静音一会儿",
                )
                return
            if self.flood_mute_all_senders and self._is_flood_out(session):
                self._mute(event, text, "本会话机器人近期发言过于频繁，先静默一会儿")
                return
            return

        prefix = self._is_allowed_prefix(text)
        if prefix:
            if self.block_llm:
                event.should_call_llm(True)
            self.passed_total += 1
            if self.verbose:
                logger.info(f"[self_guard] 放行（前缀 {prefix}）：{text[:40]}")
            return

        if not self._in_scope(event):
            return

        self._record_self_hit(session)
        blocked, reason = self.judge(session, text)
        if blocked:
            self.blocked_total += 1
            # ★ 2026-11-14：以前这里无条件 stop_event()，`active_stop` 只对 flood 那条
            #   路径生效，配置写了等于没写。统一走 _quiet：stop 由 active_stop 决定，
            #   不 stop 时仍会 should_call_llm(True) 掐掉自动回复。
            self._quiet(event, text, reason, stop=self.active_stop)
            return

        self.passed_total += 1
        if self.block_llm:
            event.should_call_llm(True)
        if self.verbose:
            logger.info(f"[self_guard] 放行但禁用 LLM：{text[:40]}")

    # ------------------------------------------------ 命令

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("自转守卫")
    async def self_guard_status(self, event: AstrMessageEvent):
        """查看自消息守卫配置与统计。"""
        lines = [
            "自消息守卫状态：",
            f"开关：{'开' if self.enabled else '关'}　模式：{self.mode}",
            f"放行前缀：{self._allow_prefixes() or '无'}",
            f"会话白名单：{self._whitelist() or '全部会话'}",
            f"范围：群 {'开' if _cfg(self.config, 'apply_group', True) else '关'} / "
            f"私聊 {'开' if _cfg(self.config, 'apply_private', True) else '关'}",
            f"放行消息禁用 LLM：{'是' if self.block_llm else '否'}",
            f"节流：{'每人每会话 %.1f 秒一次' % self.throttle_sec if self.throttle_sec else '关闭'}",
            f"已拦截 {self.blocked_total} 条，静默 {self.muted_total} 条，放行 {self.passed_total} 条",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("自转守卫日志")
    async def self_guard_log(self, event: AstrMessageEvent):
        """查看最近 10 条拦截记录。"""
        if not self._records:
            yield event.plain_result("暂时没有拦截记录。")
            return
        recent = list(self._records)[-10:]
        yield event.plain_result("最近拦截记录：\n" + "\n".join(recent))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("自转守卫测试")
    async def self_guard_test(self, event: AstrMessageEvent, text: str = ""):
        """只判定不拦截，看看某句话会不会被拦。"""
        sample = text.strip() or event.message_str.replace("自转守卫测试", "", 1).strip()
        if not sample:
            yield event.plain_result("用法：/自转守卫测试 要测试的文字")
            return
        if self._is_allowed_prefix(sample):
            yield event.plain_result("这条会因命中放行前缀而被放行。")
            return
        session = str(getattr(event, "unified_msg_origin", "") or "")
        blocked, reason = self.judge(session, sample)
        if blocked:
            yield event.plain_result(f"会被拦截：{reason}")
        else:
            yield event.plain_result("不会拦截（但若非前缀放行，默认也不会送进 LLM）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("自转守卫清空")
    async def self_guard_clear(self, event: AstrMessageEvent):
        """清空拦截记录与统计。"""
        self._records.clear()
        self._seen_self.clear()
        self._out_ts.clear()
        self._mute_log_at.clear()
        # 这几个以前没清：节流时间、学到的对端机器人、回环计数、已发送记录
        self._throttle_at.clear()
        self._learned_bots.clear()
        self._loop_hits.clear()
        self._peer_ts.clear()
        self._sent.clear()
        self.blocked_total = 0
        self.passed_total = 0
        self.muted_total = 0
        yield event.plain_result("记录和统计已清空。")

    @filter.command("自转守卫帮助")
    async def self_guard_help(self, event: AstrMessageEvent):
        """自消息守卫帮助。"""
        yield event.plain_result(
            "自消息守卫：拦下机器人自己账号发出的消息，避免自问自答。\n"
            "/自转守卫　　　查看配置与统计\n"
            "/自转守卫日志　最近 10 条拦截记录\n"
            "/自转守卫测试　判断一句话会不会被拦\n"
            "/自转守卫清空　清空记录与统计\n"
            "/自转守卫帮助　本条说明",
        )

    async def terminate(self):
        logger.info(
            f"[self_guard] 已卸载，共拦截 {self.blocked_total} 条自消息。",
        )
