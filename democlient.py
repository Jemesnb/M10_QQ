#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示客户端 —— 不连服务器、不登 QQ，直接喂假消息。

存在的理由：调试界面、排版、输入法的时候，不该每次都去登真 QQ。
一个号登两处会把别处挤下线，而且真群里的消息你没法控制 ——
想测"超长群名""@ 一个陌生人""60 字不换行的长句"，
在真号上要碰运气，在这里想造什么造什么。

它实现 OneBotClient 的**同一个接口**，所以 app.py 一行都不用改，
换一下 config.DEMO_MODE 就行：

    client = OneBotClient(...) if not DEMO else DemoClient()

⚠️ 假数据的**值**可以随便编，但**形状**必须和真的一样 ——
   形状编错了就变成"测了个假的"，本地全绿、真机一塌糊涂。
   真机踩过的那个坑（unihiker 坐标存在 item.arg 不在 .kw，
   假件顺着想象的样子长）就是前车之鉴。所以这里的每条消息
   都是照真实 OneBot 11 payload 的字段摆的，少了字段宁可不写。
"""

import random
import threading
import time

from onebot import Event, ReplyInfo, at_token


# ---------------------------------------------------------------- 假数据
#
# 群名故意有长有短、有中文有英文有 emoji —— 排版问题都是被这些逼出来的。
DEMO_GROUPS = {
    "894859599": "异世界卡车群",
    "112233445": "NorthStarCraft 用户交流反馈群",     # 22 字符，会超宽
    "556677889": "雨云RAINYUN.COM心灵驿站",            # 中英混排
    "998877665": "文件传输助手",
    "667788990": "第114514号超长群名片备用测试群",      # 长到必须截断
}

# (群号, 群名片) -> 名字。成员表就用它填。
DEMO_MEMBERS = {
    ("894859599", "3890902844"): "RC已回归",
    ("894859599", "10001"): "夜行者",
    ("894859599", "10002"): "清风明月",
    ("894859599", "10003"): "群主大人",
    ("112233445", "20001"): "张三",
    ("112233445", "20002"): "李四",
    ("556677889", "30001"): "卖瓜的",
    # 998877665 是"文件传输助手"—— 那个会话用来测换行和空消息。
    # 这几条如果不写，界面上会显示成"某人"，看着像 bug 其实只是没登记。
    ("998877665", "10001"): "我自己",
    ("998877665", "10002"): "文件助手",
}

# QQ号 -> 好友名。对应真客户端的 get_friend_list。
#
# ⚠️ 这里的好友**故意一条消息都没有** —— 演示模式的价值之一就是看
# "没说过话的好友能不能出现在列表里、能不能点进去发消息"。
# 全是空会话，正好测这个。
DEMO_FRIENDS = {
    "10001": "夜行者",
    "50001": "从没聊过的好友",          # 空会话，专门测"能主动发消息"
    "50002": "另一个没聊过的",
    "50003": "老张（备注）",
}

# 每类是 (群号, 发送者QQ号, 正文段, 额外的消息段)
# 正文段用 OneBot 的段格式，**不是**纯字符串 —— 这样走的是真解析路径
DEMO_MESSAGES = [
    # --- 普通消息 ---
    ("894859599", "10001", [{"type": "text", "data": {"text": "在吗"}}], {}),
    ("894859599", "10002",
     [{"type": "text", "data": {"text": "晚上一起打游戏不"}}], {}),

    # --- @ 成员表里有的（应该显示群名片）---
    ("894859599", "10001",
     [{"type": "at", "data": {"qq": "3890902844"}},
      {"type": "text", "data": {"text": " 你看这个"}}], {}),

    # --- @ 成员表里没有的（应该显示 @…，**不能露 QQ 号**）---
    ("894859599", "10003",
     [{"type": "at", "data": {"qq": "88888888"}},
      {"type": "text", "data": {"text": " 快来看"}}], {}),

    # --- @全体成员 ---
    ("894859599", "10003",
     [{"type": "at", "data": {"qq": "all"}},
      {"type": "text", "data": {"text": " 都出来开会"}}], {}),

    # --- 一条 @ 两个人（考验占位符带序号）---
    ("894859599", "10002",
     [{"type": "at", "data": {"qq": "3890902844"}},
      {"type": "at", "data": {"qq": "88888888"}},
      {"type": "text", "data": {"text": " 你们俩看看"}}], {}),

    # --- 图片 / 表情 / 语音占位 ---
    ("112233445", "20001",
     [{"type": "image", "data": {}},
      {"type": "text", "data": {"text": " 这图笑死"}}], {}),
    ("112233445", "20002",
     [{"type": "face", "data": {"id": "178"}},
      {"type": "text", "data": {"text": " 哈哈哈"}}], {}),
    ("112233445", "20001",
     [{"type": "record", "data": {}}], {}),

    # --- emoji（必须被 clean() 换成 □，否则 Tk 直接崩）---
    ("112233445", "20002",
     [{"type": "text", "data": {"text": "今天西瓜🍉很甜哦😀"}}], {}),

    # --- 超长消息（要折行）---
    ("556677889", "30001",
     [{"type": "text", "data": {"text": "这是一条很长的消息" * 8}}], {}),

    # --- 单行超长英文数字（考验按像素折行，按字符数会溢出）---
    ("556677889", "30001",
     [{"type": "text", "data": {
         "text": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}], {}),

    # --- 引用消息（带 raw.records，省一次 get_msg）---
    ("894859599", "10001",
     [{"type": "reply", "data": {"id": "749123"}},
      {"type": "text", "data": {"text": "同意"}}],
     {"records": [{"sender": {"user_id": "10002"},
                   "elements": [{"textElement": {"content": "明天开会吗"}}],
                   "sendNickName": "清风明月"}]}),

    # --- 引用消息（**没有** raw.records，要走后台 get_msg 兜底）---
    ("894859599", "10002",
     [{"type": "reply", "data": {"id": "749999"}},
      {"type": "text", "data": {"text": "收到"}}], {}),

    # --- 换行 ---
    ("998877665", "10001",
     [{"type": "text", "data": {"text": "第一行\n第二行\n第三行"}}], {}),

    # --- 空消息（只有段没有文字）---
    ("998877665", "10001", [], {}),
]


class DemoClient(object):
    """假客户端，接口和 OneBotClient 一致。

    只实现 app.py 真正用到的那些：
        state / last_error / online / groups
        start() / stop() / poll() / send()
        lookup_group() / member_name() / group_name()
        on_groups / on_names 回调
    """

    def __init__(self, on_log=None, speed=1.0):
        self.on_log = on_log or (lambda _m: None)
        self.speed = speed            # 时间倍率，>1 就是慢放

        # 直接就是"已连上"状态 —— 测的就是界面，不是连接逻辑
        self.state = "online"
        self.last_error = ""
        self.self_id = 999            # 假装机器人是 999
        self.online = True

        self.groups = dict(DEMO_GROUPS)
        self.friends = dict(DEMO_FRIENDS)

        self._queue = []
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._idx = 0

        # 回调：app.py 会覆盖掉
        self.on_groups = None
        self.on_names = None
        self.on_friends = None
        self.on_log = on_log or (lambda _m: None)

        self.sent = []                # 发出去的都记下来，方便核对

    # ---- 生命周期 ----

    def start(self):
        if self._thread is not None:
            return
        self._stop = False
        self._thread = threading.Thread(target=self._feed_loop)
        self._thread.daemon = True
        self._thread.start()
        self.on_log("演示模式：不连服务器，喂假消息")
        self.on_log("群 %d 个（含超长/中英混排），好友 %d 个"
                    % (len(self.groups), len(self.friends)))

        # ⚠️ 名单要**主动通知** —— 真客户端是连上后异步拉到
        # get_group_list / get_friend_list 再回调的，演示模式没有
        # 那个过程，不在这里补一下的话，"不用等消息就能看到群和好友"
        # 这条路径在演示模式下永远走不到。
        for cb in (self.on_groups, self.on_friends):
            if cb:
                try:
                    cb()
                except Exception as exc:
                    self.on_log("演示模式回调出错: %r" % (exc,))

    def stop(self):
        self._stop = True

    # ---- 喂消息 ----

    def _feed_loop(self):
        """每隔一会儿丢一条假消息进队列，循环往复。

        ⚠️ 用的是和真客户端**同一个** parse_event 路径，不是自己拼
        Event 对象 —— 否则测的就不是真实解析逻辑了。
        """
        from onebot import parse_event

        time.sleep(1.5)               # 让界面先画出来
        while not self._stop:
            # ⚠️ 变量名要和元组顺序**对上**。这里踩过一次：元组是
            # (群号, 发送者QQ号, …)，我解包成了 (kind, sid, …) 然后
            # 拿 kind 当发送者、sid 当群号用 —— 群和人是反的，
            # 界面上会话名全成了"群 10001"（那个号是发送者的）。
            # 名字起错不会报错，只是会安静地换错。
            gid, sender_uid, segs, extra = DEMO_MESSAGES[
                self._idx % len(DEMO_MESSAGES)]
            self._idx += 1

            name = DEMO_MEMBERS.get((gid, sender_uid), "某人")
            payload = {
                "post_type": "message",
                "message_type": "group",
                "group_id": int(gid),
                "user_id": int(sender_uid),
                "self_id": self.self_id,
                "sender": {"nickname": name, "card": name},
                "message": segs,
                # raw_message 是 CQ 码形式的原文，真事件里一定有
                "raw_message": self._to_cq(segs),
                "time": int(time.time()),
            }
            if extra:
                payload["raw"] = extra
                # raw.records 里的发送者也要喂成员表，否则引用里的 @ 没名字
                for r in extra.get("records", []):
                    s = r.get("sender") or {}
                    if s.get("user_id") and r.get("sendNickName"):
                        self.feed_member(gid, s["user_id"], r["sendNickName"])

            ev = parse_event(payload, self.self_id, self._member_map())
            if ev is not None:
                self._push(ev)

            time.sleep(3.0 / max(0.1, self.speed))

    @staticmethod
    def _to_cq(segs):
        """拼一个像样的 raw_message（真事件里长这样）。"""
        out = []
        for s in segs:
            t = s.get("type")
            d = s.get("data") or {}
            if t == "text":
                out.append(d.get("text", ""))
            elif t == "at":
                out.append("[CQ:at,qq=%s]" % d.get("qq", ""))
            elif t == "image":
                out.append("[CQ:image,file=x.jpg]")
            elif t == "face":
                out.append("[CQ:face,id=%s]" % d.get("id", ""))
            elif t == "record":
                out.append("[CQ:record,file=x.silk]")
            elif t == "reply":
                out.append("[CQ:reply,id=%s]" % d.get("id", ""))
        return "".join(out)

    def _member_map(self):
        """把 DEMO_MEMBERS 转成 parse_event 认的形状。"""
        return dict(DEMO_MEMBERS)

    def _push(self, ev):
        with self._lock:
            self._queue.append(ev)

    # ---- 收 ----

    def poll(self):
        with self._lock:
            return self._queue.pop(0) if self._queue else None

    # ---- 查询（app.py 要用）----

    def lookup_group(self, gid):
        """查群名，**查不到返回 None** —— 和真客户端一致。

        Store.refresh_names 靠这个"查不到返回 None"来判断要不要覆盖，
        返回占位字符串就等于把好名字盖成"群 123"。
        """
        return self.groups.get(str(gid))

    def group_name(self, gid, default=None):
        n = self.lookup_group(gid)
        if n:
            return n
        return default if default is not None else "群 %s" % gid

    # ---- 好友（对应真客户端的 get_friend_list）----

    def lookup_friend(self, uid):
        """查好友名，**查不到返回 None** —— 和 lookup_group 一致。"""
        return self.friends.get(str(uid))

    def friend_name(self, uid, default=None):
        n = self.lookup_friend(uid)
        if n:
            return n
        return default if default is not None else str(uid)

    def load_friends(self):
        """真客户端会去调 get_friend_list；演示模式数据已经在了。

        留这个方法是为了**接口一致** —— app.py 不分辨是真还是演示，
        少一个方法就会在某个不起眼的路径上 AttributeError。
        """
        if self.on_friends:
            try:
                self.on_friends()
            except Exception as exc:
                self.on_log("on_friends 回调出错: %r" % (exc,))

    def _on_friend_list(self, resp):
        """真客户端用它解析 get_friend_list 的响应。

        演示模式没有响应可解析，但方法要在 —— 测试会比对两边的方法名。
        """
        data = (resp or {}).get("data") or []
        for f in data:
            if isinstance(f, dict) and f.get("user_id"):
                nm = f.get("remark") or f.get("nickname")
                if nm:
                    self.friends[str(f["user_id"])] = nm
        if self.on_friends:
            try:
                self.on_friends()
            except Exception:
                pass

    def member_name(self, gid, uid):
        """查群名片，查不到返回 None（界面显示 @…，不显示号码）。"""
        return DEMO_MEMBERS.get((str(gid), str(uid)))

    def member_lookup(self, gid):
        memo = {}

        def lookup(q):
            q = str(q)
            if q not in memo:
                memo[q] = self.member_name(gid, q)
            return memo[q]
        return lookup

    def missing_names(self, gid, ids):
        return [q for q in (ids or []) if not self.member_name(gid, q)]

    def feed_member(self, gid, uid, name):
        if gid and uid and name:
            DEMO_MEMBERS[(str(gid), str(uid))] = name
            return True
        return False

    def update_members(self, gid, data):
        n = 0
        for m in data or []:
            if isinstance(m, dict) and m.get("user_id"):
                nm = m.get("card") or m.get("nickname")
                if nm and self.feed_member(gid, m["user_id"], nm):
                    n += 1
        return n

    # ---- 发 ----

    def send(self, kind, sid, text, callback=None):
        """发消息 —— 不真的发，记下来并假装成功。

        记下来是有用的：测输入界面时能确认"我打的东西确实到了发送这一步"。
        """
        self.sent.append((kind, sid, text))
        self.on_log("【演示】假装发送 %s/%s: %r" % (kind, sid, text))
        if callback:
            try:
                callback({"status": "ok", "retcode": 0,
                          "data": {"message_id": 12345}})
            except Exception:
                pass
        return True

    def fetch_reply(self, message_id, timeout=8.0):
        """引用兜底 —— 演示模式下给一条固定的，别真去等超时。"""
        return "（演示）这条引用内容没拿到", "某人"

    def fetch_member_name(self, gid, uid, timeout=8.0):
        return self.member_name(gid, uid)


def make_demo(on_log=None, speed=1.0):
    return DemoClient(on_log=on_log, speed=speed)
