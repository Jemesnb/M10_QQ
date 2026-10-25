# -*- coding: utf-8 -*-
"""OneBot 11 协议客户端（正向 WebSocket）。

OneBot 11 是 QQ 机器人框架的通用协议，NapCat / Lagrange / go-cqhttp 都实现它，
所以这个客户端不绑死 NapCat —— 以后换框架只要改地址就行。

事件长这样（收到）：
    {"post_type":"message", "message_type":"group", "group_id":123,
     "user_id":456, "sender":{"nickname":"张三"},
     "raw_message":"你好", "time":1696000000}

发消息（发出）：
    {"action":"send_group_msg", "params":{"group_id":123,"message":"hi"},
     "echo":"1"}
    {"action":"send_private_msg", "params":{"user_id":456,"message":"hi"},
     "echo":"2"}

依赖 websocket-client（纯 Python，3.7 能装）：
    pip3 install websocket-client
"""

import json
import re
import ssl
import threading
import time

try:
    import websocket          # websocket-client
except ImportError:           # 允许在没有依赖时 import 本模块做测试
    websocket = None


# ---- @ 的显示名 ----
#
# 消息段里的 at 只带一个 QQ 号，**不带名字**：
#     {"type":"at","data":{"qq":"123456"}}
# 直接显示就是 "@123456" —— 屏幕小，一串数字还占地方，用户明确说了
# 不要显示号码。所以名字要从两个地方补：
#     1. 成员表缓存（每条群消息把 sender 的群名片喂进去，抄
#        C:\Users\19565\Desktop\python\qq 的 feedAtName()）
#     2. 查不到就 get_group_member_info 现查，查到再回填
#
# 问题是名字是**异步**到的，而消息早就存进 Store 了。所以正文里先埋一个
# 占位符，名字到了再按占位符回填 —— 不能用 "@…" 当占位符，那样
# 一条消息 @ 两个人就分不清谁是谁了。

# 占位符里存的是**序号**不是 QQ 号 —— 这是故意的。
# 存号码的话，万一哪天有个显示路径漏了 resolve_at，屏幕上就是一串号码；
# 存序号的话就算漏了也只会显示出 ⟨at:0⟩ 这种一看就知道是内部标记的东西。
# 序号对应 ev.at_list 里的下标。
AT_TOKEN_RE = re.compile(r"⟨at:([0-9]+)⟩")


def at_token(idx):
    """@ 的占位符。**只存在于内存里的 ev.text_raw**，不画到屏幕上。"""
    return "⟨at:%d⟩" % idx


def resolve_at(text, ids, names):
    """把 @ 占位符换成显示名；查不到退成 @…，**绝不显示 QQ 号**。

    ids   是这条消息里 @ 的 QQ 号列表（占位符的序号是它的下标）
    names 可以是 {QQ号: 群名片}，也可以是 names(QQ号) -> 群名片 的函数
    """
    if not text or "⟨" not in text:
        return text
    ids = ids or []
    if callable(names):
        lookup = names
    elif names:
        lookup = names.get
    else:
        lookup = lambda _q: None                      # noqa: E731

    def sub(m):
        try:
            qq = ids[int(m.group(1))]
        except (ValueError, IndexError):
            return "@…"
        who = lookup(qq)
        # 查不到显示 @… —— 宁可不知道是谁，也不把号码摊在屏幕上
        return "@%s" % who if who else "@…"

    # ⚠️ 这里替换出来的东西**不带尾空格**。extract_text_raw 在占位符
    # 后面已经补过一个了，这里再加一个就是 "@张三  你好" 两个空格，
    # 小屏上白占 5px，看着也松垮。
    return AT_TOKEN_RE.sub(sub, text)


def has_at_token(text):
    return bool(text) and AT_TOKEN_RE.search(text) is not None


class Event(object):
    """一条收到的消息，UI 只认这个结构。"""

    __slots__ = ("kind", "sid", "name", "text", "text_raw", "at_list", "ts",
                 "self_id", "raw", "reply",
                 # 折行结果缓存（见 app.message_lines）。放在 Event 上
                 # 是因为折行要逐字符问 Tk 字宽，板子上单条 0.22 秒，
                 # 不缓存的话每来一条消息都要把整个会话重折一遍。
                 "_lines", "_lines_w")

    def __init__(self, kind, sid, name, text, ts, self_id, raw=None):
        self.kind = kind          # "group" 或 "private"
        self.sid = sid            # group_id 或 user_id（会话标识）
        self.name = name          # 发送者显示名
        self.text = text          # 纯文本内容（**渲染用**，@ 已尽量解析）
        self.ts = ts              # 时间戳
        self.self_id = self_id    # 机器人自己（用来判断是不是我发的）
        self.raw = raw

        # 带 @ 占位符的原文。@ 的名字是异步补的（成员表可能还没有这个人，
        # 得现查 API），补到之后要拿它重新解析 —— 见 refresh_at_names()。
        # 没有 @ 的消息它是 None，渲染层完全不用管这个字段。
        self.text_raw = None

        # 这条消息 @ 的 QQ 号，占位符的序号就是这里的下标。
        # 普通消息是空列表；都不带 @ 时就是空跑一趟，不额外分配。
        self.at_list = []

        # 被引用的那条消息（ReplyInfo），没有引用时是 None。
        # 内容可能拿不到，拿不到时界面显示"引用了消息"而不是留空白。
        self.reply = None

        # 折行缓存：None = 还没折过。改 text 时必须清掉（refresh_at_names）。
        self._lines = None
        self._lines_w = None

    def is_self(self):
        return self.name == "我"

    def at_ids(self):
        """这条消息 @ 了谁（QQ 号列表）。"""
        return list(self.at_list)

    def __repr__(self):
        return "<Event %s %s %s: %s>" % (self.kind, self.sid, self.name,
                                         self.text[:20])


class ReplyInfo(object):
    """被引用的那条消息。

    单独一个类而不是往 Event 上堆，是因为它**可能会变** —— raw.records
    里拿不到时，后台 get_msg 回来后要原地补全。而 Event 上的
    reply_text / reply_sender 是渲染时拍的快照，后台线程改它不安全，
    改这个对象才行。

    sender_id 也要留着：补全的不知道最后是哪条路径（raw.records 的
    sendMemberName、get_msg 的 card/nickname），而 @ 的名字得靠
    (群号, QQ号) 去成员表查 —— 群号在 Event.sid 上，QQ 号只有这里有。
    """

    __slots__ = ("message_id", "text", "sender", "sender_id", "done")

    def __init__(self, message_id):
        self.message_id = message_id
        self.text = ""            # 被引用消息的正文
        self.sender = ""          # 发送者显示名（可能为空）
        self.sender_id = None     # 发送者 QQ 号（可能拿不到）
        self.done = False         # 已经从服务器补过了

    def label(self):
        """给界面用的一行文本。**任何字段缺失都要有兜底文案**，
        不能留空白 —— 空白引用框会让人以为界面坏了。"""
        who, body = self.sender, self.text
        if who and body:
            return "%s: %s" % (who, body)
        if body:
            return body
        if who:
            return "%s 的消息" % who
        return "引用了消息"


def extract_text_raw(message, raw_message):
    """把消息段拍平成纯文本，@ 做成**占位符**而不是显示名。

    返回 (文本, at 的 QQ 号列表)。

    ⚠️ 这一步**不能**直接把 @ 定死成某个名字。at 段里只有 QQ 号，
    名字要么从成员表查、要么得现调 API，都是异步的；而这条消息
    下一秒就要画到屏幕上了。所以先埋占位符，名字到了再回填。
    （抄 C:\\Users\\19565\\Desktop\\python\\qq 那张 atNameMap 的思路，
    区别是那边只做到"当前消息"，所以冷启动/没说过话的人还是显示号码。）
    """
    if isinstance(message, str):
        return message, []
    if not isinstance(message, list):
        return raw_message or "", []

    parts = []
    at_ids = []
    prev_at = False
    for seg in message:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        data = seg.get("data") or {}
        if t == "text":
            body = str(data.get("text", ""))
            # @ 后面自己补了一个空格，紧跟的正文往往也带一个（QQ 发的
            # raw_message 就是 "[CQ:at,qq=x] 你好"，正文段以空格开头），
            # 不去重就是 "@张三  你好" 两个空格，白占 5px。
            if prev_at and body.startswith(" "):
                body = body[1:]
            parts.append(body)
            prev_at = False
        elif t == "image":
            parts.append("[图片]")
            prev_at = False
        elif t == "face":
            parts.append("[表情]")
            prev_at = False
        elif t == "record":
            parts.append("[语音]")
            prev_at = False
        elif t == "video":
            parts.append("[视频]")
            prev_at = False
        elif t == "file":
            parts.append("[文件]")
            prev_at = False
        elif t == "at":
            qq = str(data.get("qq") or "")
            if not qq:
                # 拿不到号就别硬编，给个中性占位
                parts.append("@某人 ")
            elif qq == "all":
                # @全体成员 没有名字可查，直接定死
                parts.append("@所有人 ")
            else:
                parts.append(at_token(len(at_ids)) + " ")
                at_ids.append(qq)
            prev_at = True
        elif t == "reply":
            # 引用由 ReplyInfo 单独渲染，正文里不留 [回复] 占位
            pass
        else:
            parts.append("[%s]" % t)
            prev_at = False
    text = "".join(parts).strip()
    if not text:
        text = raw_message or ""
    return text, at_ids


def extract_text(message, raw_message, reply_placeholder="[回复]"):
    """拍平成**能直接显示的**文本 —— @ 的名字已经解析过。

    这是给"不关心 @ 名字、只要一段文字"的调用方用的（比如 get_msg
    取被引用消息、测试）。真正收消息那条路走 extract_text_raw()，
    因为那条路的 @ 名字要异步补。
    """
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return raw_message or ""

    parts = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        data = seg.get("data") or {}
        if t == "text":
            parts.append(str(data.get("text", "")))
        elif t == "image":
            parts.append("[图片]")
        elif t == "face":
            parts.append("[表情]")
        elif t == "record":
            parts.append("[语音]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "file":
            parts.append("[文件]")
        elif t == "at":
            qq = str(data.get("qq") or "")
            if data.get("name"):
                # 这个协议实现直接给了名字，那就用
                parts.append("@%s " % data["name"])
            elif qq == "all":
                parts.append("@所有人 ")
            elif qq:
                parts.append("@… ")
            else:
                parts.append("@某人 ")
        elif t == "reply":
            if reply_placeholder:
                parts.append(reply_placeholder)
        else:
            parts.append("[%s]" % t)
    text = "".join(parts).strip()
    return text or (raw_message or "")


def find_reply_seg(message):
    """从消息段里找引用段，返回它的 data；没有返回 None。"""
    if not isinstance(message, list):
        return None
    for seg in message:
        if isinstance(seg, dict) and seg.get("type") == "reply":
            return seg.get("data") or {}
    return None


def reply_sender_id_from_raw(payload):
    """从 payload['raw'] 里挖被引用消息发送者的 QQ 号。

    raw.records[0] 就是个完整的 OneBot 消息，sender.user_id 就在里面。
    挖不到返回 None —— 那这个名字就补不了，界面显示 @…。
    """
    raw = payload.get("raw") or {}
    records = raw.get("records") or []
    if not records:
        return None
    r0 = records[0] or {}
    sndr = r0.get("sender") or {}
    return sndr.get("user_id") or r0.get("user_id")


def reply_from_raw_records(payload):
    """从 payload['raw']['records'] 里取被引用消息，返回 (文本, 发送者)。

    这是兜底的**第一层** —— 有些 NapCat 配置会直接把被引用的消息塞在
    raw.records 里，省一次 get_msg 往返。拿不到就返回 ("", "")，
    由上层决定要不要再调 get_msg。
    """
    raw = payload.get("raw") or {}
    records = raw.get("records") or []
    if not records:
        return "", ""
    r0 = records[0] or {}
    text = ""
    for el in (r0.get("elements") or []):
        te = el.get("textElement") or {}
        if te.get("content"):
            text = te["content"]
            break
    sender = r0.get("sendNickName") or r0.get("sendMemberName") or ""
    return text, sender


def make_lookup(names, gid):
    """把"成员表"包成 lookup(QQ号) -> 名字。

    names 有三种形态，接收方不用关心是哪一种：
      · None / 空        → 什么都查不到（消息进来时成员表还没建）
      · {(群号, QQ号): 名字}   ← OneBotClient.members 的原样
      · {QQ号: 名字}          ← 测试和一次性场景用着方便

    ⚠️ 必须带群号。成员表是 (群号, QQ号) 两级的 —— 不同群里同一个
    QQ 号是两个人，拿全局表直接查会张冠李戴。
    """
    if not names:
        return lambda _q: None                        # noqa: E731
    if callable(names):
        return names
    # 两种形态靠 key 的类型区分：元组 = 带群号，标量 = 不带
    sample = next(iter(names), None)
    if isinstance(sample, tuple):
        return lambda q: names.get((str(gid), str(q)))     # noqa: E731
    return lambda q: names.get(str(q))                     # noqa: E731


def parse_event(payload, self_id=None, names=None):
    """把 OneBot 事件转成 Event；不是消息事件返回 None。

    names 是成员表 —— 有的话当场就把 @ 解析掉。没有也不影响：
    text_raw 里留着占位符，名字到了再回填（见 refresh_at_names）。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("post_type") != "message":
        return None

    kind = payload.get("message_type") or "private"
    if kind == "group":
        sid = payload.get("group_id")
    else:
        sid = payload.get("user_id")

    sender = payload.get("sender") or {}
    # 群里优先显示群名片，没有就用昵称
    name = sender.get("card") or sender.get("nickname") or str(
        payload.get("user_id", "?"))

    uid = payload.get("user_id")
    if self_id is not None and uid == self_id:
        name = "我"

    # @ 先做成占位符（名字可能是异步到的），随即拿现有成员表解析一遍。
    message = payload.get("message")
    raw_text, at_ids = extract_text_raw(message, payload.get("raw_message"))
    ev = Event(kind, sid, name, "", payload.get("time") or time.time(),
               payload.get("self_id") or self_id, payload)
    ev.at_list = at_ids

    if at_ids:
        # 群号要先按自己的会话查一遍（成员表是 (群号, QQ号) 两级的），
        # 别拿一个全局表去查，不同群里同一个 QQ 号是不同的人。
        lookup = make_lookup(names, sid)
        text = resolve_at(raw_text, at_ids, lookup)
        # 有人没解析出来才留原文等回填 —— 全解析出来了就不用再惊动
        # 后台（群里 @ 很常见，这个省得实在）。
        if any(not lookup(q) for q in at_ids):
            ev.text_raw = raw_text
    else:
        text = raw_text

    ev.text = text or "[非文本消息]"

    # ---- 引用内容：三层兜底，任何一层拿到就不往下走 ----
    #   1. raw.records（NapCat 有时直接塞进来，零成本）
    #   2. get_msg（要发一次 API 调用，由调用方在后台线程补）
    #   3. 都没有 → 界面显示"引用了消息"，不留空白
    seg = find_reply_seg(message)
    if seg is not None:
        info = ReplyInfo(seg.get("id"))
        rtext, rsender = reply_from_raw_records(payload)
        info.text = rtext
        info.sender = rsender
        info.sender_id = reply_sender_id_from_raw(payload)
        ev.reply = info
        # 第 2 层留给 OneBotClient 在后台补（改写同一个 ReplyInfo）
    return ev


class OneBotClient(object):
    """后台线程跑 WebSocket，收到的消息塞进队列，UI 用 poll() 取。"""

    # _pending 里最多留多少个在途回调。超了从最老的开始丢 ——
    # 正常不会有几十个请求同时在路上，到这个量说明早就有回不来的了。
    PENDING_MAX = 32

    def __init__(self, host, port, token="", on_log=None, tls=False,
                 insecure_tls=False):
        self.host = host
        self.port = port
        self.token = token
        self.tls = tls
        # 默认**校验**证书。以前开 wss 时无条件 CERT_NONE，等于把
        # 中间人保护关掉了 —— 现在用 ws 没影响，但将来换 wss 会踩。
        # 真要连自签证书的服务器，显式传 insecure_tls=True。
        self.insecure_tls = insecure_tls
        self.on_log = on_log or (lambda _m: None)

        self.queue = []
        self.qlock = threading.Lock()

        self.state = "idle"       # idle / connecting / online / error
        self.last_error = ""
        self.self_id = None       # 机器人自己的 QQ 号

        # 群号 -> 群名。连上后拉一次 get_group_list 填进来。
        # 群消息只带 group_id，不带群名，不缓存的话界面上只能显示
        # "群 894859599" 这种没法看的东西。
        self.groups = {}
        self.group_lock = threading.Lock()
        # 有群名后回调，让界面刷新标题
        self.on_groups = None

        # QQ号 -> 好友昵称。连上后拉 get_friend_list。
        # 干嘛用的：**没说过话的好友也要能在列表里看到**，否则用户
        # 想主动给某个人发消息，得等对方先开口（那就本末倒置了）。
        self.friends = {}
        self.friend_lock = threading.Lock()
        # 好友列表到了的回调（界面拿它把会话建出来）
        self.on_friends = None

        # (群号, QQ号) -> 群名片。@ 显示名从这儿查。
        # 抄 C:\Users\19565\Desktop\python\qq 的 feedAtName：**每条群消息**
        # 都把发送者喂进来 —— 群里说话的人基本都会被 @ 到，攒一会儿
        # 大部分就都有了，不用一个一个打电话问服务器。
        # 字典操作在 CPython 里是原子的，但"检查 + 写入"不是，
        # 跨线程读写还是加锁。
        self.members = {}
        self.member_lock = threading.Lock()
        # 名字补回来之后的回调（界面拿它刷新已渲染的 @）
        self.on_names = None
        # 引用内容补回来之后的回调。
        # ⚠️ 引用是**异步**补的（后台线程去 get_msg），补到之后必须通知
        # 界面重画 —— 不然屏幕上永远是"↩ 引用了消息"。
        # 更要命的是消息有折行缓存（app.message_lines），不主动让缓存
        # 失效的话，重画也还是旧内容。
        self.on_reply = None
        # 正在查的 (群号, QQ号)，别重复发请求 —— 群里一条消息 @ 同一
        # 个人十次，那就是十次 API 调用
        self._pending_names = set()
        # 查过但查不到的（退群了、号注销了）。这类人**只问一次**，
        # 否则群里每 @ 他一次就白跑一趟 API。
        self._dead_names = set()

        self._ws = None
        self._stop = False
        self._thread = None
        self._echo = 0
        self._echo_lock = threading.Lock()
        self._pending = {}        # echo -> 回调（暂存 API 响应）

    # ---- 群名 ----

    def lookup_group(self, gid):
        """查群名，**查不到返回 None**（不返回占位）。

        用它来刷新会话名 —— 拿占位去覆盖等于没刷。
        """
        with self.group_lock:
            return self.groups.get(str(gid))

    def group_name(self, gid, default=None):
        """拿群名用于显示；没缓存到就返回 default（或 '群 <号>'）。"""
        name = self.lookup_group(gid)
        if name:
            return name
        return default if default is not None else "群 %s" % gid

    # ---- 好友 ----

    def lookup_friend(self, uid):
        """查好友昵称，**查不到返回 None**（和 lookup_group 一致）。

        返回 None 而不是占位，是为了让调用方能区分"真有个叫这名字的"
        和"没查到"。
        """
        with self.friend_lock:
            return self.friends.get(str(uid))

    def friend_name(self, uid, default=None):
        name = self.lookup_friend(uid)
        if name:
            return name
        return default if default is not None else str(uid)

    def _on_friend_list(self, resp):
        """get_friend_list 回来了 —— 填缓存并通知界面建会话。"""
        data = (resp or {}).get("data") or []
        if not isinstance(data, list):
            return
        got = {}
        for f in data:
            if not isinstance(f, dict):
                continue
            uid = f.get("user_id")
            if uid is None:
                continue
            # 备注优先，其次昵称 —— 备注是用户自己起的，更认得出来
            nm = f.get("remark") or f.get("nickname")
            if nm:
                got[str(uid)] = nm
        if got:
            with self.friend_lock:
                self.friends.update(got)
            self.on_log("好友缓存 %d 个" % len(got))
            if self.on_friends:
                try:
                    self.on_friends()
                except Exception as exc:
                    self.on_log("on_friends 回调出错: %r" % (exc,))

    def load_friends(self):
        """拉好友列表。连上后调一次；失败也不影响收消息。"""
        self._call("get_friend_list", {}, self._on_friend_list)

    # ---- 成员表（@ 的显示名）----

    def feed_member(self, gid, uid, name):
        """把一条消息里的发送者喂进成员表。gid/uid 为 None 时忽略。"""
        if gid is None or uid is None or not name:
            return False
        key = (str(gid), str(uid))
        with self.member_lock:
            if self.members.get(key) == name:
                return False
            self.members[key] = name
        return True

    def member_name(self, gid, uid):
        """查群名片。查不到返回 None —— **不要返回 QQ 号**，
        调用方拿到 None 会显示 @…，绝不把号码画到屏幕上。"""
        if gid is None or uid is None:
            return None
        with self.member_lock:
            return self.members.get((str(gid), str(uid)))

    def missing_names(self, gid, ids):
        """这批 QQ 号里，哪些是**值得去查**的（表里没有、也没查失败过）。

        别直接把"表里没有"当成"要查" —— 退群的人永远查不到，
        每被 @ 一次就白跑一趟 API。
        """
        out = []
        with self.member_lock:
            for q in ids or []:
                key = (str(gid), str(q))
                if key in self.members or key in self._dead_names:
                    continue
                out.append(q)
        return out

    def member_lookup(self, gid):
        """给一条消息用的查询函数：lookup(QQ号) -> 名字 或 None。

        和 member_name 的区别是**绑定了群号** —— resolve_at 只认
        QQ 号，群号得在闭包里带上；而且一次消息只建一个，多个 @ 共用。
        """
        memo = {}

        def lookup(q):
            q = str(q)
            if q in memo:
                return memo[q]
            nm = self.member_name(gid, q)
            memo[q] = nm
            return nm

        return lookup

    def update_members(self, gid, data):
        """把 get_group_member_list 的结果灌进成员表。返回新增/变更的条数。

        这个方法存在的唯一理由是**别让整群列表白白扔掉**：任何时候
        有人拉了一次成员列表（现在没有，但以后加管理功能就会拉），
        结果顺手就进缓存了，@ 的名字立刻齐全。
        """
        changed = 0
        for m in data or []:
            if not isinstance(m, dict):
                continue
            uid = m.get("user_id")
            nm = m.get("card") or m.get("nickname")
            if uid is None or not nm:
                continue
            if self.feed_member(gid, uid, nm):
                changed += 1
        return changed

    def fetch_member_name(self, gid, uid, timeout=8.0):
        """现查一个人的群名片（阻塞，只能在后台线程调）。

        ⚠️ 用 get_group_member_info 单查，**不要**用 get_group_member_list
        —— 后者一次把几百人的资料全拉回来，几百 KB 到几 MB 的 JSON，
        在 512MB 的板子上纯粹是浪费。这里只要一个名字。

        拿不到返回 None（调用方会显示 @…，不会显示号码）。
        """
        box = []
        done = threading.Event()

        def on_resp(resp):
            try:
                data = (resp or {}).get("data") or {}
                if isinstance(data, dict):
                    nm = data.get("card") or data.get("nickname")
                    if nm:
                        box.append(nm)
            finally:
                done.set()

        ok = self._call("get_group_member_info",
                        {"group_id": gid, "user_id": uid,
                         "no_cache": False}, on_resp)
        if not ok:
            return None
        done.wait(timeout)
        if not box:
            return None
        self.feed_member(gid, uid, box[0])
        return box[0]

    # ---- 生命周期 ----

    def start(self):
        if self._thread is not None:
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop = True
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    # ---- 收 ----

    def poll(self):
        """取一条待处理事件；没有返回 None。"""
        with self.qlock:
            return self.queue.pop(0) if self.queue else None

    def _push(self, item):
        with self.qlock:
            self.queue.append(item)
            # 别让队列无限长
            if len(self.queue) > 500:
                del self.queue[:200]

    # ---- 连接 ----

    def _url(self):
        scheme = "wss" if self.tls else "ws"
        return "%s://%s:%d/" % (scheme, self.host, self.port)

    def _run(self):
        if websocket is None:
            self.state = "error"
            self.last_error = "没装 websocket-client，跑 pip3 install websocket-client"
            self.on_log(self.last_error)
            return

        delay = 3
        while not self._stop:
            self.state = "connecting"
            try:
                header = {}
                if self.token:
                    header["Authorization"] = "Bearer " + self.token
                sslopt = {}
                if self.tls and self.insecure_tls:
                    sslopt["cert_reqs"] = ssl.CERT_NONE
                self._ws = websocket.create_connection(
                    self._url(), timeout=20, header=header,
                    sslopt=sslopt if sslopt else None)
                self.state = "online"
                self.last_error = ""
                delay = 3
                self.on_log("已连上 %s" % self._url())

                # 重连等于"世界可能变了"：退群的人回来了、新人进群了，
                # 之前查不到的人现在可能有名字了。所以把"查失败过"的
                # 名单清掉重新给一次机会。成员表本身留着 —— 名字不会
                # 因为断个线就失效，清掉反而要重新攒。
                with self.member_lock:
                    self._dead_names.clear()
                # 断线期间在途的请求永远回不来了，回调留着就是垃圾。
                # 重连是"世界可能变了"，旧连接的回调即使侥幸回来也不该用。
                with self._echo_lock:
                    self._pending.clear()

                # 连上先问一下自己是谁，用来区分"我发的"
                self._call("get_login_info", {}, self._on_login_info)
                # 群名缓存：消息里只有群号，没这个界面上只能显示数字
                self.load_groups()
                # 好友列表：**没说过话的好友也要能看到**，
                # 否则想主动找人得等对方先开口
                self.load_friends()

                while not self._stop:
                    try:
                        raw = self._ws.recv()
                    except websocket.WebSocketTimeoutException:
                        # ⚠️ 超时 ≠ 断开。OneBot 只在**有事件时**才推数据，
                        # 群里没人说话就一直没有字节。之前把超时当断开处理，
                        # 结果每 20 秒重连一次，重连那几秒的消息全丢。
                        # 这里只当"暂时没消息"，顺便 ping 一下探活 ——
                        # 连接真的半死了，会在 ping 或下一轮 recv 时暴露。
                        self._ws.ping()
                        continue
                    if not raw:
                        break
                    self._handle_raw(raw)

            except Exception as exc:
                if self._stop:
                    break
                self.state = "error"
                self.last_error = "%s" % (exc,)
                self.on_log("断开: %s（%d 秒后重连）" % (exc, delay))
                time.sleep(delay)
                delay = min(delay * 2, 60)
            finally:
                try:
                    if self._ws is not None:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None

        self.state = "idle"

    def _handle_raw(self, raw):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return

        # API 响应（有 echo）走另一个分支
        if "echo" in payload:
            cb = self._pending.pop(str(payload.get("echo")), None)
            if cb:
                try:
                    cb(payload)
                except Exception as exc:
                    self.on_log("回调出错: %r" % (exc,))
            return

        # 每收到一条群消息，顺手把发送者的群名片喂进成员表 ——
        # 群里说话的人大概率就是会被 @ 到的人，攒着比现查便宜得多。
        if payload.get("message_type") == "group":
            sndr = payload.get("sender") or {}
            self.feed_member(payload.get("group_id"), payload.get("user_id"),
                             sndr.get("card") or sndr.get("nickname"))

        ev = parse_event(payload, self.self_id, self.members)
        if ev is not None:
            self._push(ev)
            # 下面两件都是网络活，**绝不能在这个线程里同步做** ——
            # 这儿是收消息的循环，堵住就不收消息了（群里消息一来一回很快，
            # 卡一次可能就丢几条）。
            #
            # 1) 引用了消息但 raw.records 里没有内容 → 后台补
            if ev.reply is not None and not (ev.reply.text or ev.reply.sender):
                t = threading.Thread(target=self._fill_reply, args=(ev,))
                t.daemon = True
                t.start()
            # 2) @ 了成员表里没有的人 → 后台查名字
            #    只对群消息做。私聊的 sid 是**对方 QQ 号**不是群号，
            #    拿它当群号去查会往成员表里灌一条 (对方QQ号, QQ号) 的
            #    垃圾记录。（私聊正常也不会有 at 段。）
            if ev.kind == "group":
                missing = self.missing_names(ev.sid, ev.at_ids())
                if missing:
                    t = threading.Thread(target=self._fill_at_names,
                                         args=(ev.sid, missing))
                    t.daemon = True
                    t.start()
        # 其他事件（心跳、通知）忽略

    def _fill_reply(self, ev):
        """补全一条消息的引用内容，拿到后原地改 ev.reply，并通知界面。

        ⚠️ 两件事都要做，缺一不可：
          1. 把折行缓存清掉（ev._lines = None）—— 引用内容变了，
             之前折好的那一行就作废了，不清的话屏幕上还是旧的那一行
          2. 调 on_reply 让界面重画 —— 不通知的话要等下一条消息
             到达才顺带刷出来，等于"引用永远慢一句话"
        """
        info = ev.reply
        if info is None or info.done:
            return
        info.done = True
        text, sender = self.fetch_reply(info.message_id)
        if not (text or sender):
            return
        if text:
            info.text = text
        if sender:
            info.sender = sender
        ev._lines = None                  # 折行缓存作废
        if self.on_reply:
            try:
                self.on_reply()
            except Exception as exc:
                self.on_log("on_reply 回调出错: %r" % (exc,))

    def _fill_at_names(self, gid, ids):
        """后台把缺失的 @ 名字查回来，然后通知界面刷新。

        查不到就查不到 —— 界面显示 @…，绝不退回显示 QQ 号。
        """
        got = False
        for uid in ids:
            key = (str(gid), str(uid))
            with self.member_lock:
                if key in self._pending_names or key in self._dead_names:
                    continue
                self._pending_names.add(key)
            try:
                if self.fetch_member_name(gid, uid):
                    got = True
                else:
                    # 查不到（退群了 / 号被注销）就记下来，别再问第二遍。
                    # 不记的话，群里每有人 @ 他一次就发一次 API 调用，
                    # 板子这点算力经不起这么耗。
                    with self.member_lock:
                        self._dead_names.add(key)
            except Exception as exc:
                self.on_log("查群名片失败 %s/%s: %r" % (gid, uid, exc))
            finally:
                with self.member_lock:
                    self._pending_names.discard(key)
        if got and self.on_names:
            try:
                self.on_names()
            except Exception as exc:
                self.on_log("on_names 回调出错: %r" % (exc,))

    def _on_login_info(self, resp):
        data = resp.get("data") or {}
        if data.get("user_id"):
            self.self_id = data["user_id"]
            self.on_log("登录账号: %s" % data.get("nickname", self.self_id))

    # ---- 群名 ----

    def _on_group_list(self, resp):
        data = (resp or {}).get("data") or []
        if not isinstance(data, list):
            return
        got = {}
        for g in data:
            if not isinstance(g, dict):
                continue
            gid = g.get("group_id")
            if gid is None:
                continue
            # 自己的群备注优先，其次群名
            name = g.get("group_remark") or g.get("group_name")
            if name:
                got[str(gid)] = name
        if got:
            with self.group_lock:
                self.groups.update(got)
            self.on_log("群名缓存 %d 个" % len(got))
            if self.on_groups:
                try:
                    self.on_groups()
                except Exception as exc:
                    self.on_log("on_groups 回调出错: %r" % (exc,))

    def load_groups(self):
        """拉群列表填缓存。连上后调一次；失败也不影响收消息。"""
        self._call("get_group_list", {}, self._on_group_list)

    # ---- 引用消息 ----

    def fetch_reply(self, message_id, timeout=8.0):
        """调 get_msg 拿被引用消息的 (文本, 发送者)。

        ⚠️ 这是**阻塞**调用，只能在后台线程里用，绝不能在 UI 主循环里直接调
        —— 服务器慢一点界面就卡死。

        拿不到就返回 ("", "")，让调用方走兜底显示，别留空白引用框。
        """
        if not message_id:
            return "", ""
        box = []
        done = threading.Event()

        def on_resp(resp):
            try:
                data = (resp or {}).get("data") or {}
                if not data:
                    return
                # 被引用消息里的 @ 也要解析。
                # ⚠️ 这里必须**当场**解析掉，不能留占位符：这段文本是
                # 拿来当标签用的（还要截成 120 字塞进一行），没有"稍后
                # 回填"的机会，漏了就会在屏幕上出现 ⟨at:0⟩。
                # 查不到就退成 @…，不显示号码。
                sndr = data.get("sender") or {}
                who = sndr.get("card") or sndr.get("nickname") or ""
                gid = data.get("group_id")
                raw_text, at_ids = extract_text_raw(data.get("message"), "")
                text = resolve_at(raw_text, at_ids, self.member_lookup(gid))
                box.append((text[:120], who))
            finally:
                done.set()

        if not self._call("get_msg", {"message_id": str(message_id)}, on_resp):
            return "", ""
        done.wait(timeout)
        return box[0] if box else ("", "")

    # ---- 发 ----

    def _call(self, action, params, callback=None):
        if self._ws is None:
            return False
        # echo 要在锁里取 —— 主线程（用户发消息）和后台线程（补引用、
        # 拉群名）会同时调 _call，`_echo += 1` 撞了就会有两个请求用同一个
        # echo，回调互相错配。
        with self._echo_lock:
            self._echo += 1
            echo = str(self._echo)
            if callback:
                self._pending[echo] = callback
                # 超过这个数就清掉最老的一批 —— 正常在途请求不会有几十个，
                # 到这个量说明早就有回不来的了（服务器没响应 / 断线）。
                if len(self._pending) > self.PENDING_MAX:
                    for k in list(self._pending)[:len(self._pending)
                                                 - self.PENDING_MAX]:
                        self._pending.pop(k, None)
        try:
            self._ws.send(json.dumps({
                "action": action, "params": params, "echo": echo}))
            return True
        except Exception as exc:
            # 发都发不出去，回调永远等不到响应 —— 当场摘掉，
            # 否则它会在 _pending 里躺到程序退出
            if callback:
                with self._echo_lock:
                    self._pending.pop(echo, None)
            self.on_log("发送失败: %r" % (exc,))
            return False

    def send(self, kind, sid, text, callback=None):
        """发消息。kind 是 'group' 或 'private'。"""
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"
        return self._call(action, {key: sid, "message": text}, callback)

    @property
    def online(self):
        return self.state == "online"
