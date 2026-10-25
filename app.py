#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 终端 —— 行空板上收/发 QQ 消息。

NapCat 跑在服务器上，这块板子只做「显示 + 简单输入」。
重活（维持 QQ 长连接、协议加密）都在服务器那边，板子只连 OneBot 11。

两个界面：
    会话列表  ——  最近有消息的会话，带最后一条预览
    聊天界面  ——  某个会话的消息，点右侧弹快捷回复

按键/触摸（和阅读器保持一致，记性成本低）：
    A/B 键        上下移动 / 翻页
    点左 1/3      上一页
    点右 1/3      下一页
    点中间 1/3    弹出操作菜单（聊天页是快捷回复）
    ✕（右上角）   退出

用法（在板子上）：
    DISPLAY=:0 XAUTHORITY=/var/run/lightdm/root/:0 python3 app.py
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config
from onebot import OneBotClient, resolve_at
from textbuffer import TextBuffer
from pinyin_ime import PinyinIME, best_freq
from voice import make_engine
from cloud_asr import make_cloud

BG = "#0E1116"
FG = "#D8DEE6"
DIM = "#7D8590"
TRACK = "#21262D"
ACCENT = "#58A6FF"
OK = "#3FB950"
BAD = "#F85149"
PANEL = "#161B22"
SELF = "#7EE787"          # 自己发的消息用不同颜色

W, H = 240, 320
PAD = 8

# 排版实测值，别按字号猜（字宽≈字号的 1.4 倍，行高≈2 倍）
FONT = 12
TITLE_FONT = 14          # 标题栏字号。比正文宽 17%，截断要按它自己的宽度算
LINE_H = 24
CHARS_PER_LINE = 13      # 仅作为"至少能放几个汉字"的下限估计，别用来截断
BODY_TOP = 46
BODY_LINES = 9

# 正文可用宽度：左边距到右边距
TEXT_W = W - PAD * 2

LIST_ROW_H = 22
LIST_TOP = 46
LIST_ROWS = 9

VIEW_SESSIONS = "sessions"
VIEW_CHAT = "chat"
VIEW_MENU = "menu"
VIEW_REPLY = "reply"
VIEW_INPUT = "input"

# 聊天页的操作菜单。「打字发送」是新增的第一项 ——
# 快捷回复保留在后面，点一下最快的那条路不能砍。
MENU_ITEMS = ["打字发送", "快捷回复", "回到会话列表", "断开重连", "退出程序"]


# ---------------------------------------------------------------- 输入界面布局
#
# 2.8" 的屏，每像素约 0.178mm。26 键在竖屏下每键 24px ≈ 4.3mm，
# 只有成人指尖（约 9mm）的一半 —— 会误触，这是硬约束，不是没调好。
# 所以配了大号退格、光标键、撤销来兜底。
#
# 纵向分配（总高 320）：
#     0..36     标题栏
#     36..140   文本区（4 行 × 24px，光标按像素定位）
#     140..164  候选栏（只有中文组字时才出现）
#     164..320  键盘（4 行 × 39px）
#
# 横向每行都凑满 240，不留凑不齐的空隙 —— 手指不会掉到缝里。

KB_TOP = 164
KB_ROW_H = 39
TEXT_TOP = 40
TEXT_LINES = 4
CAND_TOP = 140

# 候选栏。屏宽 240，左留 20px 给 `‹`，右边到 `›`（226）之前可用 204px。
#
# ⚠️ 从 6×33 改成 5×40：中文字宽 17px，**两个字的词要 34px**，
# 33px 的格子装不下，fit_text 会直接截成「…」—— 用户看到一排省略号，
# 等于没候选。40px 放得下两个字（34px + 6px 余量）。
# 代价是一页少一个候选，翻页补回来。
CAND_COUNT = 5
CAND_X0 = 20
CAND_W = 40
# 一次算多少个候选。5 页够翻的了 —— 'mao' 有 60 多个同音字，
# 全算出来也没人翻到底，白白占 CPU（板子是弱鸡）。
CAND_MAX = CAND_COUNT * 5

# 隐藏控件时把它们挪到这儿 —— 屏幕（240×320）外面。
# 藏起来不靠 state 的原因见 _show_input_ui。
HIDE_X, HIDE_Y = 400, 400


def _toggle_item(item, show, hx, hy):
    """显示或隐藏一个 canvas item。两套办法一起上。

    ⚠️ 不能只靠 state：unihiker 的 config() 签名里没有 state
    （靠 **kw 转给 canvas.itemconfigure），而且**矩形是它自己按 fill
    决定 state 的**（GUI.py 里 `if fill != '': state='normal' else:
    state='hidden'`）—— 我们设的会被它覆盖掉。

    ⚠️ 回位要用**调用方记下的 hx/hy**，不能问 item 要。unihiker 把
    位置存在 item.arg 里，item.kw 里没有 x/y —— 去读 kw 会得到 0，
    回位时整套控件全糊到左上角（真机上踩过）。
    """
    # ⚠️ 合并成**一次** config 调用。输入界面有 100 多个控件，
    # 每次显隐都逐个调两次的话，板子上光这一项就要一秒多 ——
    # 用户体感是"页面出来了但要等一两秒才点得动"。
    try:
        if show:
            item.config(state="normal", x=hx, y=hy)
        else:
            item.config(state="hidden", x=HIDE_X, y=HIDE_Y)
    except Exception:
        # 有的 item 不吃 state（unihiker 的矩形自己按 fill 设），
        # 退回只挪位置 —— 挪到屏幕外一样等于隐藏
        try:
            item.config(x=hx, y=hy) if show else item.config(x=HIDE_X, y=HIDE_Y)
        except Exception:
            pass

# 键盘四态，左下角那个键**循环**切换：
#
#     中文 → 英文 → 中文字符 → 英文字符 → 中文 …
#
# 前两个是字母键盘（只是上屏方式不同），后两个是字符键盘
# （数字 + 标点，中英文各一套）。
#
# ⚠️ 四页的**键位几何必须完全一样**（每行 10/9/9/4 个键、每个多宽），
# 切页时只换键帽上的字和动作码，不重排坐标。
# 为什么：键盘控件是启动时一次性建好的，位置还被 _show_input_ui
# 记在 input_items 里（收起/展开要按原样摆回去）。一旦切页时改了
# 某个键的宽度，后面所有键的 x 都会平移，那些记下来的坐标就全错了
# —— 从输入界面返回时整个键盘会错位。
#
# 几何（宽度之和 = 240）：
#   行1  10 个 × 24
#   行2   9 个 × 24，左右各缩 12
#   行3  36 + 7×24 + 36
#   行4  48 + 48 + 96 + 48
#
# 每行 [(显示字, 宽px, 动作码), ...]

KB_MODES = ("cn", "en", "cn_sym", "en_sym")
# 左下角键上显示的当前状态。两个字的话 48px 放得下（17px × 2 = 34px）
KB_MODE_LABEL = {"cn": "中", "en": "英", "cn_sym": "中符", "en_sym": "英符"}
# 切到该状态时标题栏给的提示
KB_MODE_HINT = {"cn": "中文", "en": "英文",
                "cn_sym": "中文符号", "en_sym": "英文符号"}

_KB_LETTERS = [
    [(c, 24, ("ch", c)) for c in "qwertyuiop"],
    [(c, 24, ("ch", c)) for c in "asdfghjkl"],
    ([("⇧", 36, ("shift", None))]
     + [(c, 24, ("ch", c)) for c in "zxcvbnm"]
     + [("⌫", 36, ("back", None))]),
    [("中", 48, ("mode", None)),
     ("语音", 48, ("voice", None)),
     ("空格", 96, ("space", None)),
     ("发送", 48, ("send", None))],
]

_KB_CN_SYM = [
    [(c, 24, ("ch", c)) for c in "1234567890"],
    [(c, 24, ("ch", c)) for c in "，。？！、：；（）"],
    ([("《", 36, ("ch", "《"))]
     + [(c, 24, ("ch", c)) for c in ["》", "…", "“", "”", "·", "￥", "～"]]
     + [("⌫", 36, ("back", None))]),
    [("中符", 48, ("mode", None)),
     ("语音", 48, ("voice", None)),
     ("空格", 96, ("space", None)),
     ("发送", 48, ("send", None))],
]

_KB_EN_SYM = [
    [(c, 24, ("ch", c)) for c in "1234567890"],
    # 9 个 —— 行2 有 12px 缩进，10 个会顶出屏幕（240+12 > 240）
    [(c, 24, ("ch", c)) for c in "@#$&*-+=/"],
    ([("~", 36, ("ch", "~"))]
     + [(c, 24, ("ch", c)) for c in ["^", "'", '"', "(", ")", "[", "]"]]
     + [("⌫", 36, ("back", None))]),
    [("英符", 48, ("mode", None)),
     ("语音", 48, ("voice", None)),
     ("空格", 96, ("space", None)),
     ("发送", 48, ("send", None))],
]

KB_PAGES = {
    "cn": _KB_LETTERS,
    # 英文页和中文页**共用同一套字母键**，只是上屏方式不同（见 _do_key）
    "en": _KB_LETTERS,
    "cn_sym": _KB_CN_SYM,
    "en_sym": _KB_EN_SYM,
}

KB_ROW2_INDENT = 12


def wrap_with_cursor(text, cursor, width_px):
    """按像素折行，并算出**光标画在哪**。

    返回 (lines, cursor_row, cursor_x)：
        lines      每行的文本
        cursor_row 光标在第几行（可能等于 len(lines)，表示要在末尾新起一行）
        cursor_x   光标在这一行里的像素 x

    ⚠️ 光标按**像素**定位，不按字符数 —— 中文 17px、ASCII 11px，
    按字符数算光标会落在字中间（这个坑在截断那里已经踩过一次）。
    """
    lines = []
    row = 0
    x = 0
    cur_row = 0
    cur_x = 0
    line = ""
    for i, ch in enumerate(text):
        if i == cursor:
            cur_row = row
            cur_x = x
        w = text_width(ch)
        if x + w > width_px and line:
            lines.append(line)
            row += 1
            line = ch
            x = w
        else:
            line += ch
            x += w
    if cursor >= len(text):
        cur_row = row
        cur_x = x
    lines.append(line)
    return lines, cur_row, cur_x


# ---------------------------------------------------------------- 会话存储

class Store(object):
    """按会话聚合消息。key = (kind, sid)。

    ⚠️ 会话**不只从消息来**。群里没人说话、好友没找过你的时候，
    列表里也得能看到他们 —— 否则用户想主动发消息，得等对方先开口，
    那就本末倒置了。所以除了 add()（收消息建会话），还有
    seed()（从群列表/好友列表预建）和 ensure()（按需建空会话）。
    """

    def __init__(self, group_names=None, friend_names=None):
        self.sessions = {}        # key -> {"name","msgs":[],"unread","ts"}
        self.order = []           # key 按最近活跃排序
        # 由 QQTerm 注入的缓存，和 client 共用同一个 dict（所以是引用）
        self.group_names = group_names if group_names is not None else {}
        self.friend_names = friend_names if friend_names is not None else {}
        # 空会话（从名单建出来的）排在活跃会话后面。
        # 用一个递减的负时间戳：越早建的越靠后，这样列表顺序稳定，
        # 不会每次刷新都跳来跳去。
        self._idle_ts = 0

    def add(self, ev):
        # 净化放在**入口**，不放显示层。所有消息都从这里进，净化一次，
        # 下游的标题、会话名、预览、正文就全都安全了 —— 以后新增显示
        # 代码也不用再想着 clean()。
        ev.name = clean(ev.name)
        # ⚠️ ev.text 里有 @ 的**占位符**（⟨at:123⟩），clean() 不会动它
        # （都是 BMP 字符），回填时用 text_raw 重新解析一遍。
        ev.text = clean(ev.text)
        if ev.reply is not None:
            ev.reply.text = clean(ev.reply.text)
            ev.reply.sender = clean(ev.reply.sender)

        # ⚠️ 必须归一化。ensure/seed 那边都做了，这里漏过一次就前功尽弃：
        # 消息里 sid 是 int、名单里是 str，同一群会建出两个会话。
        # 现在 OneBot 给的是数字，但网关/别的实现可能给字符串，
        # 入口统一一次比事后排查便宜得多。
        key = (ev.kind, self.norm_sid(ev.sid))
        s = self.sessions.get(key)
        if s is None:
            s = {"name": self._name_for(ev), "msgs": [], "unread": 0,
                 "ts": ev.ts}
            self.sessions[key] = s
        s["msgs"].append(ev)
        if len(s["msgs"]) > config.MAX_MESSAGES:
            del s["msgs"][:len(s["msgs"]) - config.MAX_MESSAGES]
        s["ts"] = ev.ts
        # 未读数由界面决定要不要算 —— 用户正开着这个会话时不该加。
        # 这里只加，清零交给 mark_read（见主循环收消息那段）。
        if not ev.is_self():
            s["unread"] += 1
        if key in self.order:
            self.order.remove(key)
        self.order.insert(0, key)

    def add_self(self, key, text):
        """自己发的消息也进列表，这样界面上能看到。

        ⚠️ 会话不存在时**不能直接 return** —— 那就是"消息发出去了、
        但界面上没有"，用户以为没发出去又发一遍。用 ensure 兜底建出来。
        （点了会话之后 add_self 一般都不会走到这里，但边界情况要有兜底。）
        """
        from onebot import Event
        self.ensure(key[0], key[1])
        s = self.sessions.get(key)
        if s is None:
            return
        ev = Event(key[0], key[1], "我", clean(text), time.time(), None)
        s["msgs"].append(ev)
        s["ts"] = ev.ts
        # 自己发的也要顶到最前面
        if key in self.order:
            self.order.remove(key)
        self.order.insert(0, key)

    # 群名是**异步**到的：连上后才拉 get_group_list，而消息可能先到。
    # 所以会话名不能只在创建时定一次，要能后补。
    def refresh_names(self, resolver):
        """群名缓存好了之后调一次，把已建的群会话名补上。

        resolver(group_id) -> 群名 或 None。
        ⚠️ resolver 必须对**未知群号返回 None**，不能返回 "群 123" 这种占位
        —— 否则拿占位名去覆盖，等于什么都没刷。所以下面按缓存查而不是
        调 client.group_name()（那个会带默认值）。
        """
        for (kind, sid), s in self.sessions.items():
            if kind != "group":
                continue
            name = resolver(sid)
            if name:
                s["name"] = clean(name)

    def _name_for(self, ev):
        if ev.kind == "group":
            # 建会话时群名可能还没拉回来，先用号码占位，
            # 等 refresh_names 补上
            name = self.group_names.get(str(ev.sid)) if self.group_names else None
            return clean(name) if name else "群 %s" % ev.sid
        # 私聊。⚠️ 自己发的消息 ev.name 是"我"，不能拿它当会话名 ——
        # 那样列表里会出现一个叫"我"的会话。先去好友表查对方的名字，
        # 查不到才退回号码。（给没聊过的好友发第一条消息会走到这里。）
        if ev.is_self():
            nm = self.friend_names.get(str(ev.sid)) if self.friend_names \
                else None
            return clean(nm) if nm else str(ev.sid)
        return ev.name

    def refresh_at_names(self, resolver):
        """@ 的名字补回来了 —— 把已存的消息重新解析一遍。

        @ 的显示名是**异步**到的（成员表里没有这个人时要现查 API），
        而消息早就进 Store 了。所以拿到名字后回头把每条带占位符的
        消息重新渲染一次，否则屏幕上永远是那条消息进来时的那一眼。

        只动有占位符的消息（text_raw 非 None），绝大多数消息扫一眼就跳过。
        返回改了几条 —— 一条都没改就不用惊动界面。

        resolver(群号, QQ号) -> 名字 或 None。
        """
        changed = 0
        for s in self.sessions.values():
            for ev in s["msgs"]:
                if not ev.text_raw:
                    continue
                old = ev.text
                missing = [q for q in ev.at_list if not resolver(ev.sid, q)]
                new = clean(resolve_at(
                    ev.text_raw, ev.at_list,
                    lambda q, _sid=ev.sid: resolver(_sid, q)))
                if new != old:
                    ev.text = new
                    # 文本变了，之前折好的行作废 —— 不清的话屏幕上
                    # 永远是 @ 还没解析出来的那一眼。
                    ev._lines = None
                    changed += 1
                if not missing:
                    # 都解析出来了，原文就没用了 —— 留着每条消息都要
                    # 重跑一遍正则，纯白发工资
                    ev.text_raw = None
        return changed

    # ---- 从名单建会话 ----

    @staticmethod
    def norm_sid(sid):
        """把会话 id 统一成**消息事件里的那种类型**。

        ⚠️ 这个坑很隐蔽，值得写清楚：

            OneBot 消息里的 group_id / user_id 是 JSON **数字** → int
            缓存和配置里的都是**字符串**（get_group_list 也返数字，
            但存进 dict 时我们写了 str(gid)）

        于是同一群会有两个不相等的 key：

            ("group", 894859599)      ← 消息建的
            ("group", "894859599")    ← 名单建的

        **结果：列表里同一个群出现两次** —— 一个带消息、一个空。
        （本地集成检查抓到的，真机上也一样。）

        统一成 int：数字形式的就转 int，转不动（比如以后可能出现
        的字符串 id）就原样保留。
        """
        if isinstance(sid, int):
            return sid
        s = str(sid)
        try:
            return int(s)
        except (TypeError, ValueError):
            return s

    def default_name(self, kind, sid):
        """按缓存给一个会话起名字。查不到就返回占位。

        ⚠️ 私聊别返回"我" —— 自己先开口给好友发消息时，
        会话名得是**对方**的名字，否则列表里冒出一个叫"我"的会话。
        """
        if kind == "group":
            nm = self.group_names.get(str(sid)) if self.group_names else None
            return clean(nm) if nm else "群 %s" % sid
        nm = self.friend_names.get(str(sid)) if self.friend_names else None
        return clean(nm) if nm else str(sid)

    def ensure(self, kind, sid, name=None):
        """确保某个会话存在（没有就建个空的）。返回 key。

        给"点开会话才发现它不在列表里"兜底用 —— 比如群列表还没拉回来
        就先点了，或者 seed 的时候名字还没有、现在有了。

        name 留空就按 default_name() 去缓存里查。

        ⚠️ 已存在就**只补名字，不动消息**。重复调用要幂等 ——
        seed、add_self、open_chat 都会调它，不能把已有消息冲掉。
        """
        sid = self.norm_sid(sid)
        key = (kind, sid)
        s = self.sessions.get(key)
        if s is None:
            self._idle_ts -= 1        # 递减，保证后建的排在同级更后面
            s = {"name": clean(name) if name else self.default_name(kind, sid),
                 "msgs": [], "unread": 0, "ts": self._idle_ts}
            self.sessions[key] = s
            # 空会话 append 到末尾 —— order 按活跃度排，空会话本来就垫底
            self.order.append(key)
        elif name and not s["msgs"]:
            # 还没说过话的会话，名字可以随时更新（群改名、备注改了）
            s["name"] = clean(name)
        return key

    def seed(self, groups=None, friends=None):
        """把群列表 / 好友列表灌进来，建出**空会话**。

        groups  {群号: 群名}
        friends {QQ号: 昵称}

        没说过话的会话 msgs 是空的，界面上预览行显示占位文字，
        但**能点进去发消息** —— 这就是这个函数的意义。
        """
        n = 0
        for gid, name in (groups or {}).items():
            before = len(self.sessions)
            self.ensure("group", gid, name)
            if len(self.sessions) > before:
                n += 1
        for uid, name in (friends or {}).items():
            before = len(self.sessions)
            self.ensure("private", uid, name)
            if len(self.sessions) > before:
                n += 1
        return n

    def has_any(self):
        return bool(self.sessions)

    def mark_read(self, key):
        if key in self.sessions:
            self.sessions[key]["unread"] = 0

    def get(self, key):
        return self.sessions.get(key)

    def ordered(self):
        """按显示顺序返回会话。

        ⚠️ 有消息的排在**前面**，纯名单建出来的空会话垫底。
        排序规则是 ts 从大到小：
            · 有消息的  ts = 最后一条消息的时间（正数，越新越大）
            · 空会话    ts = _idle_ts（负数，越早建越小）
        所以活跃会话永远在空会话上面，而空会话之间保持稳定顺序
        （不会每刷新一次就换位置，那样手指会点错）。
        """
        keys = sorted(
            (k for k in self.order if k in self.sessions),
            key=lambda k: -self.sessions[k]["ts"])
        out = []
        for key in keys[:config.MAX_SESSIONS]:
            s = self.sessions.get(key)
            if s:
                out.append((key, s))
        return out


def clean(text):
    """把文本净化成 Tk 能显示的样子。

    ⚠️ Tcl/Tk 8.6 的字符集只到 U+FFFF（BMP）。任何 emoji（😀 U+1F600、
    🍉 U+1F349、👍 …）传给 canvas.itemconfigure 都会**直接抛异常**，
    不是显示成豆腐块，是整个程序崩掉。QQ 消息里 emoji 太常见了，
    实测第二条消息就踩到：

        _tkinter.TclError: character U+1f349 is above the range
        (U+0000-U+FFFF) allowed by Tcl

    所以所有要进 Tk 的文本都必须先过这里。emoji 换成 '□'，
    零宽字符和不可见控制符直接丢掉。
    """
    if text is None:
        return ""
    out = []
    for ch in str(text):
        cp = ord(ch)
        # 换行在渲染层另有处理（wrap_text 会切段），这里保留
        if ch == "\n":
            out.append(ch)
        # 零宽字符、格式控制符、私有区：显示出来是乱码，直接丢
        elif cp == 0x200B or 0x200B <= cp <= 0x200F or cp == 0xFEFF:
            continue
        elif 0xE000 <= cp <= 0xF8FF:
            continue
        # 辅助平面（emoji 大多在这）：Tk 显示不了，用方块占位
        elif cp > 0xFFFF:
            out.append("□")
        # ⚠️ 孤立代理（U+D800–U+DFFF）也必须挡掉。
        # 正常 emoji 在 Python 里是一个字符（>0xFFFF），走上面那条；
        # 但如果服务器把代理对发**断**了（JSON 里只剩 \ud83d），
        # json.loads 会造出一个孤立的 0xD83D —— 它在 BMP 范围内，
        # 上面哪条都不匹配，于是原样送进 Tk。
        # Tcl 没法编码代理，Tk 调用直接抛异常，整个程序跟着死。
        # 这类坏包在网关/代理转发时真的会出现。
        elif 0xD800 <= cp <= 0xDFFF:
            out.append("□")
        # 其他 C0/C1 控制符
        elif cp < 0x20 and ch != "\n":
            continue
        else:
            out.append(ch)
    return "".join(out)


# ---- 文本宽度 ----
#
# ⚠️ 千万别用"截断到 N 个字符"来防溢出。中文约 17px、ASCII 约 9px，
# 同样 13 个字符的像素宽度能差一倍 —— 群名一长就冲出屏幕（实测踩过）。
# 必须按**像素**截。真机上用 Tk 的 font.measure()，离线测试时退回估算。

_measure = None          # 默认字号的测量；由 QQTerm 注入
_measure_by_size = {}    # 字号 -> 测量函数（标题是 14 号，正文是 12 号）

# 字宽缓存。(字号, 字符串) -> px
#
# ⚠️ 这不是可选优化。板子上一次 font.measure 要走一趟 Tcl，实测 4.19ms ——
# 而 wrap_text_px 是**逐字符**问宽度的，一条 44 字的消息就要 44 次 ≈ 0.18 秒。
# 折行函数还被 fit_text 反复调用，实测单条消息 0.22 秒、300 条 67 秒。
# 缓存之后同样字符只问一次（中文常用字也就几千个），折行降到纯字典查找。
_w_cache = {}
_W_CACHE_MAX = 12000


def set_measure(fn, size=FONT):
    """注入真实的字体测量函数（板子上是 tkfont.Font.measure）。

    ⚠️ 要**按字号分别注入**。标题用 14 号字、正文用 12 号，同一个字符串
    14 号比 12 号宽 17%（实测 237px vs 277px）。拿 12 号的测量去截 14 号的
    标题，算出来"装得下"，画出来就压到右边的 ✕ 上了。
    """
    global _measure
    if size == FONT:
        _measure = fn
    _measure_by_size[size] = fn
    # 换测量函数（换字体/换字号/测试注入假测量）等于之前算的全作废
    _w_cache.clear()


def text_width(s, size=FONT):
    """字符串的像素宽度。size 要和实际绘制用的字号一致。

    结果会缓存 —— 见 _w_cache 上面那段，这是板上能不能流畅的关键。
    """
    key = (size, s)
    hit = _w_cache.get(key)
    if hit is not None:
        return hit

    fn = _measure_by_size.get(size)
    if fn is None and size == FONT:
        fn = _measure
    if fn is not None:
        w = fn(s)
    else:
        # 没注入时的估算：中文/全角 ~17px，其余 ~9px。
        # 只给离线测试和没有 GUI 的场景用，估算偏保守（宁可多留白）。
        # 字号不同就按比例缩放（17/12 ≈ 1.42）。
        scale = size / float(FONT)
        w = 0
        for ch in s:
            w += 17 if ord(ch) > 0x2E7F else 9
        w = int(w * scale)

    if len(_w_cache) >= _W_CACHE_MAX:
        _w_cache.clear()      # 简单粗暴，别让长聊天下把内存吃光
    _w_cache[key] = w
    return w


def fit_text(s, width, ellipsis="…", size=FONT):
    """把字符串截到**像素宽度** width 以内，超出部分用省略号。

    按字符逐个累加像素宽，而不是按字符数 —— 见上面那段注释。
    放不下省略号就干脆不加，别为了省略号把内容再挤掉一格。

    size 必须和实际画的字号一致，否则就是假截断（见 set_measure）。
    """
    s = clean(s)
    if not s:
        return ""
    if text_width(s, size) <= width:
        return s
    ell_w = text_width(ellipsis, size)
    out = []
    used = 0
    for ch in s:
        w = text_width(ch, size)
        # 留出省略号的位置
        if used + w + ell_w > width:
            break
        out.append(ch)
        used += w
    if not out:
        # 一个字符都放不下（宽度极度受限），至少给省略号
        return ellipsis if ell_w <= width else ""
    return "".join(out) + ellipsis


def wrap_text(text, width):
    """折行。width 是**字符数**口径，保留给旧调用方；

    新代码请用 wrap_text_px() —— 字符数在混排时不准。
    """
    text = clean(text)
    if not text:
        return [""]
    out = []
    for para in text.split("\n"):
        while len(para) > width:
            out.append(para[:width])
            para = para[width:]
        out.append(para)
    return out or [""]


def wrap_text_px(text, width_px):
    """按**像素**折行。中文 ASCII 混排才能对齐。

    逐字符累加宽度，超了就断行。放不下的单个字符也要占一行，
    否则会丢字。
    """
    text = clean(text)
    if not text:
        return [""]
    out = []
    for para in text.split("\n"):
        if not para:
            out.append("")
            continue
        line = []
        used = 0
        for ch in para:
            w = text_width(ch)
            if used + w > width_px and line:
                out.append("".join(line))
                line = [ch]
                used = w
            else:
                line.append(ch)
                used += w
        out.append("".join(line))
    return out or [""]


def quote_line(ev, width_px=TEXT_W):
    """把引用内容渲染成一行，形如 `↩ 张三: 原消息`。

    拿不到被引用内容时显示 `↩ 引用了消息`，**不能留空白** ——
    这点是抄 C:\\Users\\19565\\Desktop\\python\\qq 的 renderReplyToken：
        引用内容可能因为消息太旧/get_msg 失败而拿不到，
        空白引用框会让人以为界面坏了。
    """
    if ev.reply is None:
        return None
    # 按像素截，不是按字数。兜底文案在 ReplyInfo.label() 里。
    return fit_text("↩ " + ev.reply.label(), width_px)


def message_lines(msgs, width_px=TEXT_W):
    """把消息渲染成一行行文本。

    每条消息占 1~2 行：有引用时先出一行引用，再出正文。
    返回 [(文本, 是否自己发的), ...]

    width_px 是**像素**宽度。按字符数排版在中文/ASCII 混排时会溢出 ——
    昵称带英文、正文夹数字、还有 □ 占位符，宽度差一倍是常事。

    ⚠️ 每条消息的折行结果**缓存在 ev 上**（_lines / _lines_w）。
    折一次要逐字符问 Tk 字宽，板子上实测单条 0.22 秒 —— 30 条消息
    就是 6 秒，而且每次按键、每来一条新消息都会全量重算，界面直接假死。
    消息内容只在 @ 名字补回来时才会变，那处会显式把缓存清掉。
    """
    lines = []
    for ev in msgs:
        cached = getattr(ev, "_lines", None)
        if cached is None or getattr(ev, "_lines_w", None) != width_px:
            cached = _lines_for(ev, width_px)
            ev._lines = cached
            ev._lines_w = width_px
        lines.extend(cached)
    return lines


def _lines_for(ev, width_px):
    """单条消息折成若干行。结果由 message_lines 缓存到 ev 上。"""
    name = clean(ev.name)
    is_self = ev.is_self()
    lines = []

    # 引用单独占一行（在发送者上面），不混进正文
    q = quote_line(ev, width_px)
    if q:
        lines.append((q, is_self))

    # ⚠️ 名字**必须先按像素截断**再用。QQ 群名片二三十个字符很常见，
    # 不截的话 prefix 自己就有 300+px，比整行（224px）还宽，于是：
    #   · avail = width - prefix_w 变负数，被 max(40, …) 兜成 40px
    #     → 正文一行只能放两个字，看着像一条竖线（实测踩过）
    #   · 首行 = 超长 prefix + 正文，三百多像素直接冲出屏幕
    # 给 prefix 封顶：最多占整行 40%，正文永远至少留 60%。
    # 顺序上要先给 ": " 留出位置再截名字，否则截完冒号就没了。
    # 昵称也要净化 —— QQ 昵称/群名片里带 emoji 太常见了。
    # 而且净化必须在算缩进**之前**，否则续行的缩进会跟首行对不齐
    # （emoji 被换成 1 个 '□'，长度和原来不一样）。
    colon = ": " if name else ""
    prefix_room = max(60, width_px * 2 // 5)
    prefix = fit_text(name, max(20, prefix_room - text_width(colon))) + colon

    # 缩进用"和 prefix 等宽的空格"，但空格是 ASCII（5px）而 prefix
    # 里可能有中文（17px），所以按像素算要几个空格。
    prefix_w = text_width(prefix)
    indent = " " * max(1, prefix_w // max(1, text_width(" ")))
    indent_w = text_width(indent)

    # 正文可用宽度 = 整行宽 - 缩进宽
    avail = max(40, width_px - prefix_w)
    chunks = wrap_text_px(ev.text, avail)
    for i, ch in enumerate(chunks):
        # 折行后每行要带 is_self 一起塞进去，所以整个是个 tuple。
        # 别写成 append(a + ch, ev.is_self()) —— append 只吃一个参数，
        # 会直接 TypeError（这里踩过一次）。
        lead = prefix if i == 0 else indent
        # 首行 prefix 已经吃掉 prefix_w，正文还要再截一次保险
        body_room = width_px - (prefix_w if i == 0 else indent_w)
        lines.append((lead + fit_text(ch, max(20, body_room)), is_self))
    return lines


# ---------------------------------------------------------------- 界面

class QQTerm(object):

    def __init__(self):
        from unihiker import GUI

        self.g = GUI(w=W, h=H)
        self.g.canvas.config(bg=BG)
        g = self.g

        # 注入真实的字体测量 —— 之后 fit_text / wrap_text_px 就是按像素算的，
        # 不再是"几个字符"的估算。字体是 GUI 库自己加载的那个
        # （HYQiHei_50S.ttf），所以要等 GUI 实例化之后才拿得到。
        # 每个用到的字号都要注入：12（正文/列表）和 14（标题）。
        # 字体在板子上是等宽不变的，所以只建一次可以一直用。
        self.tkfonts = {}
        try:
            from tkinter import font as tkfont
            for size in (FONT, TITLE_FONT, 11, 18):
                f = tkfont.Font(family=g.font_name, size=size)
                self.tkfonts[size] = f
                set_measure(f.measure, size)
        except Exception as exc:
            print("[warn] 字体测量注入失败，退回估算: %r" % (exc,))

        self.pending = []
        g.on_a_click(lambda: self.pending.append("a"))
        g.on_b_click(lambda: self.pending.append("b"))
        self.quit_requested = False
        try:
            g.master.bind("<Button-1>",
                          lambda e: self.pending.append(("tap", e.x, e.y)))
        except Exception:
            pass

        self.client = self._make_client()
        # Store 和 client 共用同一份缓存 —— 名字是异步到的，
        # 会话可能先建好了，得能后补。
        self.store = Store(self.client.groups, self.client.friends)
        # 回调统一在这儿挂（群名/名片/好友列表），别处不要再手写一遍
        self._wire_client()
        self.log_lines = []
        self.view = VIEW_SESSIONS
        self.cur = None           # 当前会话 key
        self.sel = 0
        self.scroll = 0           # 聊天页往回翻的行数
        self.menu_sel = 0
        self.msg = ""             # 顶部提示

        # ---- 顶部 ----
        # 标题右边界：往左让开圆点（x=192）和 ✕（x=216）。截断在
        # _draw_chat_body 里按 TITLE_FONT 实际宽度算，不是按字数。
        # 状态只用一个圆点，文字放页脚 —— 之前把"● 连接中"写在标题栏
        # 右侧，排到 x=226 正好压在 ✕ 上。
        self.title = g.draw_text(x=PAD, y=8, text="QQ", color=FG,
                                 font_size=TITLE_FONT, origin="nw")
        self.dot = g.draw_text(x=W - 48, y=10, text="●", color=DIM,
                               font_size=FONT, origin="nw")
        self.exit_btn = g.draw_text(x=W - 24, y=6, text="✕", color=DIM,
                                    font_size=18, origin="nw",
                                    onclick=self._request_quit)
        g.draw_line(x0=PAD, y0=36, x1=W - PAD, y1=36, color=TRACK, width=1)

        # ---- 正文行（两种视图共用）----
        self.rows = []
        y = BODY_TOP
        for _ in range(BODY_LINES):
            self.rows.append(g.draw_text(x=PAD, y=y, text="", color=FG,
                                         font_size=FONT, origin="nw"))
            y += LINE_H
        self.arrow = g.draw_text(x=PAD - 6, y=LIST_TOP, text="▸",
                                 color=ACCENT, font_size=FONT, origin="nw")

        # ---- 浮层（菜单 / 快捷回复共用）----
        self.box = g.fill_rect(x=0, y=0, w=0, h=0, color=PANEL)
        self.box_items = []
        for _ in range(6):
            self.box_items.append(g.draw_text(x=0, y=0, text="", color=FG,
                                              font_size=12, origin="nw"))

        # ---- 底部 ----
        #
        # ⚠️ 这三样（分隔线 + 两行提示）正压在**输入界面的键盘**上
        # （键盘占 y=164..320，提示在 264/268/292）。所以它们必须能
        # 跟着输入界面一起隐藏 —— 否则进键盘以后，底下的"点会话进入"
        # 会从键缝里透出来。收进 self.footer_items，和 input_items
        # 一起切显隐。
        self.footer_items = []
        _sep = g.draw_line(x0=PAD, y0=264, x1=W - PAD, y1=264,
                           color=TRACK, width=1)
        self.hint = g.draw_text(x=PAD, y=268, text="", color=DIM,
                                font_size=11, origin="nw")
        self.hint2 = g.draw_text(x=PAD, y=292, text="", color=DIM,
                                 font_size=11, origin="nw")
        for _it, _x, _y in ((_sep, PAD, 264), (self.hint, PAD, 268),
                            (self.hint2, PAD, 292)):
            self.footer_items.append((_it, _x, _y))
            try:
                _it.config(state="hidden")
            except Exception:
                pass

        # ---- 输入界面 ----
        self._build_input_ui(g)

        self.client.start()
        self.show_sessions()

    def _build_input_ui(self, g):
        """把输入界面的所有 canvas item 一次建好，**隐藏着**。

        ⚠️ 40 多个按键不能每次进输入界面都重新建 —— 建删 canvas item
        在板子上是有感的卡顿，而且 unihiker 的 remove 不一定立刻回收。
        照 self.rows / self.box_items 的既有写法：建一次，之后只
        config() 改文字和颜色。

        隐藏方式统一用 `state="hidden"`（比挪到屏幕外干净）。
        """
        self.buffer = TextBuffer()
        self.kb_mode = "cn"         # cn / en / cn_sym / en_sym（见 KB_MODES）
        self.kb_shift = False       # ⇧ 大写锁定（按下一次只影响一个字母）
        self.asr = None             # 语音引擎，第一次按语音键时才建
        self.cloud = None           # 云端识别引擎（没配 key 就是 None）
        self.cloud_job = None       # 正在飞的云端请求（后台线程）
        self.voice_state = "idle"   # idle / recording / finishing
        self.hint_msg = ""          # 输入界面的临时提示
        # 发送键的两步确认状态。True = 已经点过一次，再点就真发。
        # 敲任何别的键都会清掉它 —— 免得"点了一下发送发现打错字、
        # 改完再点"直接就把错字发出去了。
        self.send_armed = False

        # 全部要隐藏/显示的 item，连同它**原本的坐标**一起记下来：
        #     [(item, home_x, home_y), ...]
        #
        # ⚠️ 坐标必须自己记。一开始我是想"回位时问 item 要不就行了"，
        # 于是去读 item.kw["x"] —— 结果真机上 unihiker 把位置存在
        # item.arg 里，item.kw 里装的是 Tk 那层的东西（font/fill…），
        # 根本没有 x。读出来全是 0，回位时把所有控件都挪到了左上角。
        # 别再依赖库的内部字段，创建的时候是什么坐标自己最清楚。
        self.input_items = []

        def mk(item, hx, hy):
            self.input_items.append((item, hx, hy))
            try:
                item.config(state="hidden")
            except Exception:
                pass
            return item

        # 文本区的 4 行 + 光标竖线
        self.in_rows = []
        for i in range(TEXT_LINES):
            ry = TEXT_TOP + i * LINE_H
            self.in_rows.append(mk(g.draw_text(
                x=PAD, y=ry, text="", color=FG,
                font_size=FONT, origin="nw"), PAD, ry))
        # 光标：一条 2px 宽的竖线，位置每次重画
        self.in_cursor = mk(g.fill_rect(x=PAD, y=TEXT_TOP, w=2, h=LINE_H - 4,
                                        color=ACCENT), PAD, TEXT_TOP)

        # 候选栏：6 个候选 + 左右翻页箭头
        #
        # ⚠️ 从 4 个加到 6 个，是因为用户报"打个 mao 根本点不到冒" ——
        # 'mao' 有 60 多个同音字，4 个位置连常用字都装不下。
        # 现在 6 个 + 翻页，第 2 页也能点到。
        #
        # 横向分配：左留 20px 给 ‹，右边 226 处是 ›，中间 204px 放
        # 5 个候选（每个 40px）。40px 是为了**放得下两个字的词** ——
        # 33px 会把「测试」截成「…」，见上面 CAND_W 的注释。
        self.in_cands = []
        self.cand_page = 0            # 候选翻到第几页
        for i in range(CAND_COUNT):
            cx = CAND_X0 + i * CAND_W
            self.in_cands.append(mk(g.draw_text(
                x=cx, y=CAND_TOP + 2, text="", color=ACCENT,
                font_size=FONT, origin="nw"), cx, CAND_TOP + 2))
        # 翻页箭头。只在候选多于一页时显示
        self.cand_prev = mk(g.draw_text(x=2, y=CAND_TOP + 2, text="‹",
                                        color=DIM, font_size=FONT,
                                        origin="nw"), 2, CAND_TOP + 2)
        self.cand_next = mk(g.draw_text(x=W - 14, y=CAND_TOP + 2, text="›",
                                        color=DIM, font_size=FONT,
                                        origin="nw"), W - 14, CAND_TOP + 2)

        # 键盘。三页共用**同一套控件**（几何完全一致，见 KB_PAGES 上面那段），
        # 切页只改键帽文字和动作码，不重建、不挪位置。
        self.keys = []              # [(bg, txt, 动作码, 显示字), ...]
        for r, row in enumerate(KB_PAGES[self.kb_mode]):
            x = KB_ROW2_INDENT if r == 1 else 0
            y = KB_TOP + r * KB_ROW_H
            for label, kwidth, action in row:
                # 键的背景（圆角看着舒服些）
                bg = mk(g.fill_rect(x=x, y=y, w=kwidth, h=KB_ROW_H - 2,
                                    color=PANEL), x, y)
                # 键上的字居中：按像素量一下再定位，别按字数猜
                tw = text_width(label)
                tx = x + max(0, (kwidth - tw) // 2)
                tx = max(0, min(W - tw, tx))
                ty = y + (KB_ROW_H - 2 - 18) // 2
                txt = mk(g.draw_text(
                    x=tx, y=ty, text=label,
                    color=FG, font_size=FONT, origin="nw"), tx, ty)
                self.keys.append((bg, txt, action, label))
                x += kwidth

        # ⚠️ 输入界面**没有**独立的提示行 —— 键盘一直铺到屏幕底部
        # （y=320），中间塞不下第五行。提示借用标题栏显示，见 _draw_input。
        # 之前这里写过一个 y=W-8 的提示行，那是把宽度当高度用了，
        # 结果正好压在键盘第 2 行上。

    def _show_input_ui(self, on):
        """显示/隐藏整个输入界面。

        ⚠️ 这里**不能只靠 state**。unihiker 的 `config()` 签名里没有 state
        （它靠 **kw 转给 canvas.itemconfigure），而且矩形是**它自己**
        按 fill 决定 state 的（GUI.py 里 `if fill != '': state='normal'
        else: state='hidden'`）—— 所以我们设的 state 可能被覆盖掉。

        所以两套都上：先试 state，再把它挪到屏幕外。挪位置这条路
        一定管用，因为 x/y 是 config 的正式参数。两条都不报错才算完，
        哪条真的生效了不影响正确性（都在屏幕外就等于隐藏）。

        ⚠️ 回位用的是**建的时候记下的坐标**（input_items 里的 hx/hy），
        不是问 item 要。踩过的坑：unihiker 把位置存在 item.arg 里，
        item.kw 里没有 x/y —— 去读 kw 会得到 0，回位时整套控件
        全糊到左上角（真机上就是这个后果）。
        """
        for it, hx, hy in self.input_items:
            _toggle_item(it, on, hx, hy)

        # 底部那三样要**反过来**：输入界面开着的时候藏起来
        # （它们正压在键盘上），离开输入界面才显示。
        #
        # getattr 兜底：这两个列表的顺序在建控件时是有讲究的
        # （footer 要先建），用 getattr 免得以后再调整顺序时
        # 这里 AttributeError。
        for it, hx, hy in getattr(self, "footer_items", ()):
            _toggle_item(it, not on, hx, hy)

    # ---- 事件 ----

    def _make_client(self):
        """造客户端 —— 真连服务器还是喂假消息，看 config.DEMO_MODE。

        演示模式不连网、不登 QQ，就是为了调界面的时候不用动真号
        （一个号登两处会把另一处挤下线）。两种客户端接口一致，
        下面的代码一行都不用分辨。
        """
        log = lambda m: self.pending.append(("log", m))       # noqa: E731
        if getattr(config, "DEMO_MODE", False):
            from democlient import make_demo
            return make_demo(on_log=log,
                             speed=getattr(config, "DEMO_SPEED", 1.0))
        return OneBotClient(config.SERVER_HOST, config.SERVER_PORT,
                            config.ACCESS_TOKEN, on_log=log)

    def _request_quit(self):
        """右上角 ✕ 的点击回调。

        ⚠️ 这个按钮是**全局**的 —— 它在每个界面都画在同一个位置、
        挂着同一个回调。所以必须按当前界面分派，不能一律退出程序：

            输入界面  → 收起键盘，回聊天页（**不退出程序**）
            其它界面  → 退出程序

        踩过的坑：以前这里直接 `quit_requested = True`，结果在键盘上
        打字时想关键盘，一点 ✕ 整个程序就没了，输的东西也没了。
        """
        if self.view == VIEW_INPUT:
            self.buffer.commit_compose()      # 挂着的拼音别丢
            self.close_input()
            self.show_chat()
            return
        self.quit_requested = True

    # ---- 事件分派 ----

    def dispatch(self, ev):
        """处理一个界面事件。

        ⚠️ 抽成方法是为了让主循环能 drain() 一次取完积压 —— 以前主循环
        是「poll() 取一个 + sleep(0.2)」，按住 A/B 时键重复远快于每秒 5 个，
        事件在 pending 里越积越长，松手之后界面还在往下跑。
        """
        if ev is None:
            return
        if ev == "a":
            if self.view == VIEW_SESSIONS:
                self.sel = max(0, self.sel - 1)
                self.show_sessions()
            elif self.view == VIEW_CHAT:
                self.scroll = min(self.scroll + BODY_LINES,
                                 max(0, len(self._cur_lines()) - BODY_LINES))
                self.show_chat()
            elif self.view == VIEW_INPUT:
                # 输入界面里 A/B 当左右移光标用 —— 板子上没有方向键，
                # 手指又太大点不准字缝，这两个键正好补上
                self.buffer.move_left()
                self._draw_input()
            elif self.view in (VIEW_MENU, VIEW_REPLY):
                self.menu_sel = max(0, self.menu_sel - 1)
                self._show_box(
                    (MENU_ITEMS
                     if self.view == VIEW_MENU else config.QUICK_REPLIES),
                    self.menu_sel, y0=80 if self.view == VIEW_MENU else 60)
        elif ev == "b":
            if self.view == VIEW_SESSIONS:
                n = len(self.store.ordered())
                self.sel = min(max(0, n - 1), self.sel + 1)
                self.show_sessions()
            elif self.view == VIEW_CHAT:
                self.scroll = max(0, self.scroll - BODY_LINES)
                self.show_chat()
            elif self.view == VIEW_INPUT:
                self.buffer.move_right()
                self._draw_input()
            elif self.view in (VIEW_MENU, VIEW_REPLY):
                items = (MENU_ITEMS
                         if self.view == VIEW_MENU else config.QUICK_REPLIES)
                self.menu_sel = min(len(items) - 1, self.menu_sel + 1)
                self._show_box(items, self.menu_sel,
                              y0=80 if self.view == VIEW_MENU else 60)
        elif isinstance(ev, tuple):
            if ev[0] == "tap":
                self.handle_tap(ev[1], ev[2])
            elif ev[0] == "log":
                self.log_lines.append(ev[1])
                print("[onebot] %s" % ev[1])
            elif ev[0] == "groups":
                # 群列表到了。两件事：
                #   1. 把**已有的**会话名补上（之前可能只有号码）
                #   2. 把**还没建过的**群建成空会话 —— 这就是
                #      "不用等群来消息也能看到它"那件事
                # 传"查得到才返回值"的版本，别用带占位默认值的那个。
                self.store.refresh_names(self.client.lookup_group)
                self.store.seed(groups=dict(self.client.groups))
                self.refresh_view()
            elif ev[0] == "friends":
                # 好友列表到了 —— 同样建出空会话。
                # 没说过话的好友也要能看到，否则想主动找人得等对方先开口。
                self.store.seed(friends=dict(self.client.friends))
                self.refresh_view()
            elif ev[0] == "names":
                # @ 的群名片到了：把正文里已经画出来的 @… 换成真名。
                # 这里用 refresh_view（认全五种视图），别只认 CHAT ——
                # 浮层开着的时候新消息一到就会被拆掉（踩过）。
                if self.store.refresh_at_names(self.client.member_name):
                    self.refresh_view()
            elif ev[0] == "reply":
                # 引用内容补回来了：缓存已在 onebot 那边清掉，这里重画就行。
                # 用 refresh_view（认全五种视图），别只认 CHAT。
                self.refresh_view()
            elif ev[0] == "ime":
                # 拼音索引建好了。如果此刻正在打字，把候选补上 ——
                # 用户可能已经开始敲了，之前是空的。
                if self.view == VIEW_INPUT and self.buffer.composing:
                    # ⚠️ 必须和敲键那条路一样传 limit=CAND_MAX。
                    # 不传就走默认 limit=4，而候选栏一行能显示 6 个 ——
                    # 用户已经打完字在等索引，结果只出 4 个，最后两个格子
                    # 永远是空的，看着像索引没建完。
                    self._recompute_candidates()
                    self._draw_input()

    def refresh_view(self):
        """按当前视图重绘。**所有**"有新数据了"的地方都调这个，别各写各的。

        ⚠️ 这里必须认全**五种**视图。只区分 CHAT / 非CHAT 是不够的 ——
        VIEW_REPLY 和 VIEW_MENU 是叠在聊天页上的浮层，收到新消息时
        底层聊天页要更新，但**浮层不能被拆掉**。之前就是从这儿弹回列表的。

        ⚠️ VIEW_INPUT 尤其要注意：群里消息一来一回很快，打字的时候
        被一条新消息踢出去是灾难性的（输入的内容还没发就没了）。
        所以输入界面**只刷状态提示，绝不重画文本区和键盘**。
        """
        if self.view == VIEW_SESSIONS:
            self.show_sessions()
            return
        if self.view == VIEW_INPUT:
            # 输入界面自成一体，新消息不影响它。只更新底下的连接状态。
            self._set_status()
            return
        if self.cur is None:
            self.show_sessions()
            return
        # 聊天页 / 浮层：只重画底下的内容，**不动 self.view，不清浮层**
        #
        # ⚠️ 这里**不调 _clear()**。_clear 会把 9 行正文全置空，紧接着
        # _draw_chat_body 再逐行填 —— 中间有一瞬间是空屏，表现就是
        # "新消息一来屏幕闪一下"。用户报过这个。
        # _draw_chat_body 自己会先写后清（见那里的注释），所以不需要
        # 外面再清一遍。浮层也不能清，清了这个视图就退了。
        keep = self.view
        self._draw_chat_body()
        self.view = keep
        self._restore_box()

    def _wire_client(self):
        """给当前 client 挂上回调。

        ⚠️ 抽成方法是因为这里**漏过两次**：先漏了 on_names，加好友功能时
        又在重连那条路上漏了 on_friends。初始化那处和重连那处各手写一遍
        必然会漂 —— 一边加了另一边忘。所以只留一个地方写。
        """
        p = self.pending
        self.client.on_groups = lambda: p.append(("groups", None))
        self.client.on_names = lambda: p.append(("names", None))
        self.client.on_friends = lambda: p.append(("friends", None))
        self.client.on_reply = lambda: p.append(("reply", None))

    def _restore_box(self):
        """按当前 view 把浮层摆回去（重绘正文不会碰浮层，但位置要对齐）。"""
        if self.view == VIEW_MENU:
            self._show_box(MENU_ITEMS, self.menu_sel, y0=80)
        elif self.view == VIEW_REPLY:
            self._show_box(config.QUICK_REPLIES, self.menu_sel, y0=60)

    def poll(self):
        return self.pending.pop(0) if self.pending else None

    def drain(self):
        """一次取走所有积压的界面事件。

        按住 A/B 时键重复远快于"每轮循环 sleep(0.2)"，以前每轮只取一个，
        队列只会越积越长 —— 松手之后界面还在往下跑，看着像卡死。
        现在整批取走、逐个分派。

        ⚠️ **必须原地清空，不能 `self.pending = []`**。
        `_wire_client()` 里写的是 `p = self.pending` —— 网络线程的回调
        抓着的是**这个列表对象**。一旦这里换成新列表，回调还在往旧列表
        里塞，而旧列表再没人读 —— 于是 on_groups / on_friends / on_names
        全部静默丢失：消息照收（走 client.queue），但群名永远补不上，
        好友列表不出现，列表里只能看到"群 数字"，非得等群里发消息
        才有一条会话冒出来。（这个回归就是这么来的，记牢。）
        """
        out = self.pending[:]      # 复制一份去处理
        del self.pending[:]        # 原地清空，对象身份保持不变
        return out

    def _clear(self, keep_box=False):
        """清掉正文行。keep_box=True 时保留浮层（刷新底层内容时用）。

        ⚠️ 不碰输入界面 —— 那些 item 有各自的显示/隐藏开关
        （_show_input_ui），混在一起清就会把输入内容抹掉。
        """
        for r in self.rows:
            r.config(text="")
        self.arrow.config(text="")
        if not keep_box:
            self._hide_box()

    def _hide_box(self):
        self.box.config(x=0, y=0, w=0, h=0)
        for it in self.box_items:
            it.config(text="", x=0, y=0)

    def _show_box(self, items, sel, y0=78):
        bw = 168
        bh = 26
        x0 = (W - bw) // 2
        self.box.config(x=x0, y=y0, w=bw, h=bh * len(items) + 8)
        for i, it in enumerate(self.box_items):
            if i < len(items):
                # items 可能是 config.QUICK_REPLIES（用户自己编辑的），
                # 里面放 emoji 一样会让 Tk 崩，所以这里也净化一次
                it.config(text=("▸ " if i == sel else "  ") + clean(items[i]),
                          x=x0 + 8, y=y0 + 6 + i * bh,
                          color=ACCENT if i == sel else FG)
            else:
                it.config(text="", x=0, y=0)

    # ---- 会话列表 ----

    def show_sessions(self):
        self.close_input()
        self.view = VIEW_SESSIONS
        self._clear()
        self.title.config(text="QQ")
        self._set_status()

        items = self.store.ordered()
        if not items:
            self.rows[0].config(text="等消息中…", color=DIM)
            if self.client.state == "error":
                self.rows[2].config(text="连不上服务器", color=BAD)
                self.rows[3].config(
                    text=fit_text(self.client.last_error or "", W - PAD * 2),
                    color=DIM)
            self.hint.config(text=self._state_text())
            self.hint2.config(text="在 config.py 填连接信息")
            return

        # 每个会话占 2 行：名字 + 最后一条预览
        self.sel = max(0, min(self.sel, len(items) - 1))
        shown = items[self.sel // 2 * 0:]
        # 简单起见：从 sel 对应的会话开始，最多显示 4 个会话
        start = max(0, min(self.sel, len(items) - 1))
        per_page = 4
        start = (start // per_page) * per_page
        page = items[start:start + per_page]

        y_off = 0
        for i, (key, s) in enumerate(page):
            idx = start + i
            last = s["msgs"][-1] if s["msgs"] else None
            name = s["name"]
            if s["unread"]:
                name += " (%d)" % s["unread"]
            # 按像素截断。群名可能是"第1145141919号测试群(备用)"这种
            # 中英数字混排，按字符数截必然冲出去（实测踩过）。
            # 未读数后缀也要算进宽度里，否则加了 (1) 就溢出了。
            self.rows[y_off].config(
                text=fit_text(name, TEXT_W - 2),
                color=ACCENT if idx == self.sel else FG)
            preview = ""
            if last:
                preview = "%s: %s" % (last.name, last.text)
            else:
                # 空会话（从名单建的，还没说过话）—— 预览行要有点东西，
                # 不然看着像界面坏了。也顺便告诉用户"可以点进去发"。
                preview = "群里还没人说话" if key[0] == "group" \
                    else "还没聊过 · 点进去发消息"
            # 预览是次要信息，留出右边一点点空间，别顶满
            self.rows[y_off + 1].config(text=fit_text(preview, TEXT_W - 10),
                                        color=DIM)
            y_off += 2

        # ⚠️ 用完之后把多余的行清掉（**先写后清**，同 _draw_chat_body）。
        # 会话少了不清的话，上次画的会留在下面冒充当前内容。
        for i in range(y_off, len(self.rows)):
            self.rows[i].config(text="")

        row = (self.sel - start) * 2
        self.arrow.config(y=LIST_TOP + row * (LINE_H - 2) + 2)

        self.hint.config(text="A上 B下  点会话进入")
        if self.msg:
            self.hint2.config(text=self.msg)
        else:
            self.hint2.config(text="%s · %d 个会话"
                              % (self._state_text(), len(items)))

    def _set_status(self):
        """圆点表示连接状态，文字由调用方放进页脚。"""
        st = self.client.state
        if st == "online":
            self.dot.config(color=OK)
        elif st == "connecting":
            self.dot.config(color="#D29922")
        else:
            self.dot.config(color=BAD)

    def _state_text(self):
        st = self.client.state
        if st == "online":
            return "在线"
        if st == "connecting":
            return "连接中…"
        if st == "error":
            return "离线，重试中"
        return "未连接"

    # ---- 聊天 ----

    def open_chat(self, idx):
        items = self.store.ordered()
        if not items:
            return
        # idx 是从列表点/选出来的，可能越界（列表刚变短时）
        idx = max(0, min(idx, len(items) - 1))
        self.sel = idx
        key, _s = items[idx]
        # ⚠️ 兜底 ensure：会话可能还没进 store（比如群列表刚到、
        # 名字还没补上时点的）。ensure 是幂等的，已有就什么都不做。
        self.store.ensure(key[0], key[1], _s.get("name") or None)
        self.cur = key
        self.store.mark_read(key)
        self.scroll = 0
        self.view = VIEW_CHAT
        self.show_chat()

    def show_chat(self):
        """切到聊天页（清浮层、收起输入界面）。"""
        self.close_input()
        self.view = VIEW_CHAT
        self._clear()
        self._draw_chat_body()

    def _draw_chat_body(self):
        """只画聊天页正文，**不改 self.view、不碰浮层**。

        刷新时用这个，这样浮层（发送消息 / 菜单）不会被拆掉。
        """
        s = self.store.get(self.cur)
        if s is None:
            self.show_sessions()
            return
        # 标题按像素截，别用 [:9] —— 群名里混英文数字时 9 个字符能超宽。
        # ⚠️ size 必须传 TITLE_FONT：标题画的是 14 号字，比正文宽 17%。
        # 用正文的 12 号量，量出来"装得下"，画出来就压到右边的 ✕ 上了
        # （实测：按 12 号截出 199px 的东西，14 号画出来是 224px）。
        self.title.config(text=fit_text(s["name"], W - PAD - 60,
                                        size=TITLE_FONT))
        self._set_status()

        lines = message_lines(s["msgs"])
        # scroll = 0 表示贴底（看最新），越大越往回翻
        end = max(0, len(lines) - self.scroll)
        start = max(0, end - BODY_LINES)
        page = lines[start:end]

        # ⚠️ **先写后清**，别先清后写。
        #
        # 先清（9 行全置空）再逐行填的话，中间有一瞬间屏幕是空的 ——
        # 群消息一来一回很快，表现就是"闪一下"。用户报的原话：
        # "文字刷新太快了…到后面文字闪一下，又进入刷新从上往下刷了"。
        #
        # 改成：先把新内容写进去（覆盖旧的），再把**多出来的**行清空。
        # 这样任何时刻屏幕上都有完整内容，没有空屏的那一帧。
        for i, row in enumerate(self.rows):
            if i < len(page):
                text, is_self = page[i]
                row.config(text=text, color=SELF if is_self else FG)
        # 内容不够占满屏幕时，把多余的几行清掉
        for i in range(len(page), len(self.rows)):
            self.rows[i].config(text="")

        self.hint.config(text="中点发送  左右翻页")
        if self.scroll:
            self.hint2.config(text="往回翻 %d 行 · 点右侧回底" % self.scroll)
        else:
            self.hint2.config(text="%s · 已到最新" % self._state_text())

    # ---- 菜单 ----

    def _open_menu(self):
        if self.view not in (VIEW_CHAT, VIEW_INPUT):
            return
        was_input = self.view == VIEW_INPUT
        self.menu_sel = 0
        self.view = VIEW_MENU
        self._show_box(MENU_ITEMS, self.menu_sel, y0=80)
        # 从输入界面弹出来的菜单，返回时要回输入界面（不是聊天页）
        self.menu_from_input = was_input

    def _open_replies(self):
        if config.READ_ONLY:
            self.msg = "当前是只读模式"
            self.show_chat()
            return
        self.menu_sel = 0
        self.view = VIEW_REPLY
        self._show_box(config.QUICK_REPLIES, self.menu_sel, y0=60)

    def _send(self, text):
        # 兜底 ensure：给没说过话的群/好友发消息时，会话可能还没建出来。
        # 用 ensure 而不是 get()，否则这里 return 掉就是"发出去了但界面
        # 上什么都没有"（add_self 也不会生效）。
        self.store.ensure(self.cur[0], self.cur[1])
        ok = self.client.send(self.cur[0], self.cur[1], text)
        if ok:
            self.store.add_self(self.cur, text)
            self.msg = "已发送"
        else:
            self.msg = "发送失败（没连上）"
        self.scroll = 0
        self.show_chat()

    # ---- 输入界面 ----

    def open_input(self):
        """进打字发送界面。"""
        if config.READ_ONLY:
            self.msg = "当前是只读模式"
            self.show_chat()
            return
        if self.cur is None:
            return
        self.buffer.clear()
        self.hint_msg = ""
        self.view = VIEW_INPUT
        self._clear()
        self._show_input_ui(True)
        self._ensure_ime()
        # 顺手把麦克风/模型也热上 —— 用户下一步很可能就是按语音键，
        # 而加载模型要 4.6 秒、开录音流要几百毫秒。等按下才准备，
        # 开头那几个字就录不到了（真机实测录 5 秒得 0 字节）。
        self._warm_voice()
        self._draw_input()

    def _ensure_ime(self):
        """拼音索引是**懒加载**的。

        建索引要解析 8 万条数据，在 RK3308 上不是一瞬间的事，
        不能拖慢程序启动。所以第一次进输入界面才在后台建，
        建好之前候选栏是空的，但字母照常能敲（英文输入不受影响）。
        """
        if getattr(self, "ime", None) is not None:
            return
        self.ime = PinyinIME()
        # ⚠️ 用 best_freq() 而不是 common_freq() —— 后者只有 789 个字，
        # 覆盖不到「冒」「帽」「猫」这些，它们只能按码点排，会被生僻字
        # 挤到几十位去（用户报过"打个 mao 根本点不到冒"）。
        # best_freq() 优先用 jieba 的 11580 字表。
        self.ime.build_async(
            freq=best_freq(),
            on_done=lambda _ok: self.pending.append(("ime", None)))

    def close_input(self):
        """离开输入界面，把界面元素收起来。"""
        self.send_armed = False      # 换了界面就别记着"待确认"了
        self._show_input_ui(False)
        # ✕ 恢复成"退出程序"的样子（输入界面里它是"收起键盘"，
        # 颜色被改过 —— 见 _draw_input）
        try:
            self.exit_btn.config(color=DIM)
        except Exception:
            pass
        # 云端还在飞的话，结果没地方放了 —— 丢掉
        self.cloud_job = None
        # ⚠️ 必须停录音。以前不收，离开输入界面后麦克风还一直开着：
        #   · show_sessions() 第一句就是 close_input()，所以回列表也照开
        #   · voice.py 的静音自动停要求「有识别结果」，一句话没说就永不触发
        # 结果就是麦克风无限期开着，白耗电还涉及隐私。
        self._stop_voice_if_running()

    def _stop_voice_if_running(self):
        """离开输入界面：停录音 + 关麦克风。

        ⚠️ 录音流是**一直开着**的（为了按语音键立刻能录），所以这里
        必须显式 release()，否则麦克风指示灯一直亮着。
        """
        self.voice_state = "idle"
        if self.asr is None:
            return
        try:
            self.asr.request_stop()
            self.asr.release()
        except Exception:
            pass

    def _draw_input(self):
        """重画文本区、光标、候选栏。键盘的键**不动** —— 它们是固定的。"""
        b = self.buffer
        avail = W - PAD * 2

        # 文本区。拼音（还没选字的）也要显示出来，否则用户看不见自己敲了啥
        shown = b.display()
        lines, cur_row, cur_x = wrap_with_cursor(shown, b.cursor_display_pos(),
                                                 avail)
        # 只显示最后 TEXT_LINES 行（光标永远在最后一行附近）
        if len(lines) > TEXT_LINES:
            drop = len(lines) - TEXT_LINES
            lines = lines[drop:]
            cur_row = max(0, cur_row - drop)

        for i, item in enumerate(self.in_rows):
            if i < len(lines):
                item.config(text=fit_text(lines[i], avail), color=FG)
            else:
                item.config(text="")

        # 光标：一行 2px 宽的竖线，跟着走
        cy = TEXT_TOP + min(cur_row, TEXT_LINES - 1) * LINE_H + 2
        self.in_cursor.config(x=PAD + cur_x, y=cy, w=2, h=LINE_H - 6)

        # 候选栏：只有中文组字的时候才有东西。
        # 一页放不下就翻页 —— 见 in_cands 那里的注释（'mao' 有 60 多个）。
        all_cands = b.candidates if b.composing else []
        pages = max(1, (len(all_cands) + CAND_COUNT - 1) // CAND_COUNT)
        self.cand_page = max(0, min(self.cand_page, pages - 1))
        start = self.cand_page * CAND_COUNT
        page = all_cands[start:start + CAND_COUNT]

        for i, item in enumerate(self.in_cands):
            if i < len(page):
                item.config(text=fit_text(page[i], CAND_W - 2), color=ACCENT)
            else:
                item.config(text="")

        # 翻页箭头：只有真有多页时才有意义，否则不显示（别让人白点）
        more = pages > 1
        self.cand_prev.config(text="‹" if more and self.cand_page > 0
                              else "",
                              color=ACCENT if more else DIM)
        self.cand_next.config(
            text="›" if more and self.cand_page < pages - 1 else "",
            color=ACCENT if more else DIM)
        self._cand_pages = pages

        # 语言键要显示当前状态（"中"/"英"），别让用户猜
        self._refresh_keys()
        # 提示放标题栏 —— 输入界面没有独立的提示行（键盘铺到屏幕底部，
        # 中间没地方了）。有提示就显示提示，没有就显示默认说明。
        self.title.config(
            text=fit_text(self.hint_msg or "发送消息", W - PAD - 60,
                          size=TITLE_FONT))
        # ✕ 在这个界面是"收起键盘"不是"退出程序"（见 _request_quit）。
        # 换个颜色提示一下它不是退出 —— 上方还有一条分隔线留着，
        # 点它也能收起键盘（那条线归 footer_items 管，输入界面时是藏着的，
        # 所以这里不冲突）。
        self.exit_btn.config(color=ACCENT)

    def _label_kb(self):
        """按 self.kb_mode 把键帽文字和动作码摆好（控件本身不重建）。

        页与页之间键数、键宽完全一致，所以这里只改字，位置一律不动。
        """
        flat = [k for row in KB_PAGES[self.kb_mode] for k in row]
        for i, (bg, txt, _old_action, _old_label) in enumerate(self.keys):
            label, _w, action = flat[i]
            if txt.kw.get("text") != label:
                txt.config(text=label)
            self.keys[i] = (bg, txt, action, label)

    def _refresh_keys(self):
        """按键的文字/颜色按当前状态刷一遍。

        只有语言键、⇧、以及页切换键需要变颜色，别的都是固定的 ——
        但这里统一刷，逻辑简单不容易出错。
        """
        self._label_kb()
        for bg, txt, action, label in self.keys:
            kind = action[0]
            if kind == "ch":
                # ⇧ 开着就把键帽上的字母也变大写 —— 状态要看得见，
                # 否则用户不知道现在是大写，打出来才发现
                want = label.upper() if self.kb_shift else label
                if txt.kw.get("text") != want:
                    txt.config(text=want)
                txt.config(color=FG)
            elif kind == "shift":
                txt.config(color=ACCENT if self.kb_shift else FG)
            elif kind == "page":
                txt.config(color=ACCENT)
            elif kind == "mode":
                # 左下角那个键显示**当前状态**，不是"按了会变成什么"。
                # 用户之前看到标签和实际键盘对不上，就是因为这里
                # 混了"当前"和"下一个"两种语义。
                txt.config(text=KB_MODE_LABEL[self.kb_mode], color=ACCENT)
            elif kind == "voice":
                # 三种状态一眼分清（键只有 48px，"结束中"三个字放不下）
                st = getattr(self, "voice_state", "idle")
                if self.cloud_job is not None:
                    txt.config(text="识别", color=BAD)      # 等云端返回
                elif st == "finishing":
                    txt.config(text="结束", color=BAD)
                elif st == "recording":
                    txt.config(text="停止", color=BAD)
                elif self.asr is not None and not self.asr.available():
                    txt.config(text="语音", color=DIM)
                else:
                    txt.config(text="语音", color=FG)
            elif kind == "send":
                # 三种样子，一眼要能分出来：
                #   空      灰色 —— 没东西可发
                #   有内容  绿色 —— 点了会进入确认
                #   待确认  红色 + 写"确认" —— 再点就真发出去了
                empty = self.buffer.is_empty() and not self.buffer.composing
                if empty:
                    txt.config(text="发送", color=DIM)
                elif self.send_armed:
                    txt.config(text="确认", color=BAD)
                else:
                    txt.config(text="发送", color=OK)
                bg.config(color=PANEL if not self.send_armed else "#3D1418")

    def _tap_key(self, x, y):
        """点键盘。返回 True 表示点到了键。"""
        if not (KB_TOP <= y < H):
            return False
        page = KB_PAGES[self.kb_mode]
        r = (y - KB_TOP) // KB_ROW_H
        if not (0 <= r < len(page)):
            return False
        row = page[r]
        # 行 2 有个 12px 的缩进
        x0 = KB_ROW2_INDENT if r == 1 else 0
        # 行 3 的宽度不是 24 的整数倍（36+7*24+36），要按实际宽度累加
        acc = x0
        for label, kwidth, action in row:
            if acc <= x < acc + kwidth:
                self._do_key(action)
                return True
            acc += kwidth
        return True        # 点在键盘区域内但落在缝里，也算处理过了

    def _recompute_candidates(self):
        """按当前挂着的拼音重算候选。

        ⚠️ 每个候选都要带上"吃掉几个音节"（cand_syl）—— 中文一个字
        就是一个音节，所以就是 len(词)。选字时靠它判断还剩多少拼音
        要继续拼（见 _pick_candidate）。
        """
        b = self.buffer
        ime = getattr(self, "ime", None)
        if not b.composing or ime is None:
            b.set_candidates([])
            return
        cands = ime.candidates(b.composing, limit=CAND_MAX)
        b.set_candidates(cands, [len(w) for w in cands])

    def _pick_candidate(self, i):
        """选第 i 个候选，把剩下的音节留着继续拼。

        返回 True 表示选中了。

        ⚠️ 这一步是"两个词能连着打"的关键：'nihao' 选了「你」只吃掉
        一个音节，剩 'hao' 继续出候选，用户接着选「好」；选了「你好」
        就一次吃完。以前 pick() 无条件清空拼音，所以只能一个字一个字打。
        """
        b = self.buffer
        if not (0 <= i < len(b.candidates)):
            return False
        ime = getattr(self, "ime", None)
        syls = ime.segment(b.composing) if ime is not None else None
        n = b.cand_syl[i] if i < len(b.cand_syl) else len(b.candidates[i])
        rest = ""
        if syls and 0 < n < len(syls):
            rest = "".join(syls[n:])
        if not b.pick(i, rest=rest):
            return False
        self.cand_page = 0            # 选完回候选第一页
        self._recompute_candidates()  # 有剩的就接着出候选
        return True

    def _do_key(self, action):
        kind, val = action
        b = self.buffer

        if kind == "page":
            # 老的两态页切换（123 / #+=）。现在统一走 mode 四态循环，
            # 这里留着只是兼容测试里的旧断言，不做任何事。
            return

        if kind == "ch":
            # 中文模式下字母进拼音；英文模式直接落正文
            if self.kb_mode == "cn" and val.isalpha():
                b.feed_key(val)
                # 敲了新字母就回候选第一页 —— 不重置的话，
                # 打完 "ma" 翻到第 3 页再敲 "o"，会停在一个
                # 和当前拼音对不上的页码上，候选看着像乱的
                self.cand_page = 0
                # getattr 而不是 self.ime —— 索引是懒加载的，万一
                # _ensure_ime 还没跑过（比如以后加了别的入口），
                # 这里应该是"没候选"而不是把程序打崩
                self._recompute_candidates()
            else:
                b.insert(val.upper() if self.kb_shift else val)
                # ⇧ 只影响紧跟着的那个字母，用完就弹回来（手机键盘的惯例）
                if self.kb_shift:
                    self.kb_shift = False

        elif kind == "shift":
            self.kb_shift = not self.kb_shift
            self.hint_msg = "大写" if self.kb_shift else ""

        elif kind == "space":
            # 中文组字时，空格 = 选第一个候选（输入法惯例）
            if b.composing and b.candidates:
                self._pick_candidate(0)
            else:
                b.insert(" ")

        elif kind == "back":
            b.backspace()

        elif kind == "mode":
            # 四态循环：中文 → 英文 → 中文字符 → 英文字符 → 中文
            i = KB_MODES.index(self.kb_mode)
            self.kb_mode = KB_MODES[(i + 1) % len(KB_MODES)]
            # 切出拼音档就把挂着的拼音上屏，否则它会一直飘着没法编辑
            if self.kb_mode != "cn":
                b.commit_compose()
            self.hint_msg = KB_MODE_HINT[self.kb_mode]

        elif kind == "send":
            self._send_from_input()
            return

        elif kind == "voice":
            self._toggle_voice()
            return

        # 敲了任何别的键 → 取消"待确认发送"。
        # 不取消的话会出现这种事：点一下发送（变红）→ 发现打错字 →
        # 改 → 再点发送，这时**改完的内容直接就发出去了**，
        # 用户以为还是第一次确认。
        if self.send_armed:
            self.send_armed = False
        self.hint_msg = ""
        self._draw_input()

    def _send_from_input(self):
        """发送键的动作 —— **两步确认**。

        ⚠️ 为什么要两步：发送键在右下角 48px，那是右手拇指自然搁着的
        位置，而它一点就发。用户报"发送键很容易误触" —— 打字的功夫
        拇指搭上去，消息就飞出去了。QQ 消息**发出去收不回来**，
        误触的代价比多点一下大得多。

        所以第一次点只是把键变成"确认发送"（变红），再点才真发。
        期间敲任何键都会取消这个状态。

        ⚠️ 发送前先把没选的拼音**落顶上屏**（commit_compose），
        否则用户敲了 "ni" 直接点发送，那 "ni" 就凭空消失了。
        """
        b = self.buffer
        if b.composing:
            b.commit_compose()
        text = b.text.strip()
        if not text:
            self.hint_msg = "还没输入内容"
            self._draw_input()
            return

        if not self.send_armed:
            # 第一步：进入待确认状态，不真发
            self.send_armed = True
            self.hint_msg = "再点一次发送"
            self._draw_input()
            return

        # 第二步：真发
        self.send_armed = False

        # 和 _send 一样兜底 —— 给没说过话的会话发消息时它可能还没建出来
        self.store.ensure(self.cur[0], self.cur[1])
        ok = self.client.send(self.cur[0], self.cur[1], text)
        if ok:
            self.store.add_self(self.cur, text)
            b.clear()
            self.hint_msg = "已发送"
        else:
            self.hint_msg = "发送失败（没连上）"
            self._draw_input()
            return
        self._draw_input()

    def _toggle_voice(self):
        """按语音键：没在录就开录，在录就停并出结果。

        ⚠️ 识别结果**不直接发出去**，而是落进输入框 ——
        用户明确要求"语音输入的文字要可以用键盘改"。走的是
        TextBuffer.insert，和键盘同一条路。
        """
        if self.asr is None:
            self.asr = make_engine(
                config,
                on_partial=lambda _t: None,
                on_log=lambda m: self.pending.append(("log", m)))
            # 云端引擎（没配 key 就是 None，那就只用本地）
            self.cloud = make_cloud(config)
        if not self.asr.available():
            self.hint_msg = self.asr.reason() or "语音不可用"
            self._draw_input()
            return

        # 收尾中 / 云端在飞 —— 忽略连点。用户急着点两下不代表他想再录一次。
        if self.voice_state == "finishing" or self.cloud_job:
            return

        if self.voice_state == "recording":
            # 只发信号，**立刻返回**。
            # ⚠️ 以前这里调 asr.stop()，它要 join 解码线程（最多 15 秒），
            # 主循环整个卡住 —— 用户以为没点上就再点一次，等 stop 返回后
            # 那一下才被处理，于是又开了一次录音。表现就是"按钮闪回语音、
            # 要点两三次才停"。收尾交给 _poll_voice 慢慢做。
            self.voice_state = "finishing"
            self.asr.request_stop()
            self.hint_msg = "结束录音…"
            self._draw_input()
            return

        if self.asr.start():
            self.voice_state = "recording"
            self.hint_msg = "正在听… 说完自动停"
        else:
            self.hint_msg = self.asr.error() or "开不了麦克风"
        self._draw_input()

    def _warm_voice(self):
        """后台把语音引擎准备好（不阻塞界面）。"""
        if self.asr is None:
            self.asr = make_engine(
                config,
                on_partial=lambda _t: None,
                on_log=lambda m: self.pending.append(("log", m)))
            self.cloud = make_cloud(config)
        if hasattr(self.asr, "warm"):
            self.asr.warm()

    def _finish_voice(self):
        """录音结束：拿本地文本 + 音频，决定走云端还是本地。"""
        self.voice_state = "idle"
        local = self.asr.text() if hasattr(self.asr, "text") else ""
        wav = self.asr.wav() if hasattr(self.asr, "wav") else b""

        # 没录到任何音频（麦克风没打开 / 一按一放）。
        # 不做静音检测了，所以这里只判断"有没有音频"。
        if not wav:
            self._apply_voice_text("", "没听到说话")
            return

        if self.cloud is not None and wav:
            self._start_cloud(wav, local)
        else:
            self._apply_voice_text(local, "本地")

    def _start_cloud(self, wav, local_text):
        """把录音丢给云端，**后台线程**跑，结果由 _poll_cloud 取。

        ⚠️ 不能在界面线程里直接等 HTTP —— 板子到 Deepgram 一趟 1~3 秒，
        主循环卡住的话连按钮都点不动，用户会以为死机了。
        """
        seconds = max(0.0, (len(wav) - 44)) / 2.0 / 16000.0

        # 排障：把**实际发出去的**音频存一份。
        # "1.5 秒、识别不出来"这种问题，光看日志没用 —— 得看音频本身
        # 是不是太小、是不是全是噪声、是不是被裁掉了。
        try:
            with open("/tmp/last_voice.wav", "wb") as fh:
                fh.write(wav)
        except Exception:
            pass

        peak = rms = -1
        try:
            import array
            body = wav[44:]
            a = array.array("h")
            a.frombytes(body[:len(body) // 2 * 2])
            if a:
                peak = max(max(a), -min(a))
                rms = int((sum(v * v for v in a) / float(len(a))) ** 0.5)
        except Exception:
            pass
        self.pending.append(("log", "云端识别：%.1f 秒 %dKB peak=%d rms=%d"
                             % (seconds, len(wav) // 1024, peak, rms)))
        if seconds < 0.3:
            # 没录到东西，发上去也是空结果，白等一趟
            self._apply_voice_text(local_text, "没录到声音")
            return

        job = {"done": False, "text": "", "err": "",
               "local": local_text or "", "t0": time.time()}
        self.cloud_job = job

        def run():
            try:
                job["text"] = self.cloud.transcribe(wav)
            except Exception as exc:      # AsrError 及意料外的都算失败
                job["err"] = "%s" % (exc,)
            finally:
                job["done"] = True

        t = threading.Thread(target=run)
        t.daemon = True
        t.start()
        self.hint_msg = "云端识别中…"
        self._draw_input()

    def _poll_cloud(self):
        """主循环里调：云端作业完成了就把文本落到输入框。"""
        job = getattr(self, "cloud_job", None)
        if not job:
            return
        # 用户已经离开输入界面 —— 结果没地方放，直接丢
        if self.view != VIEW_INPUT:
            self.cloud_job = None
            return
        if not job["done"]:
            waited = time.time() - job["t0"]
            # 兜底超时：HTTP 层自己会超时，这里再保一层
            if waited > float(getattr(config, "CLOUD_TIMEOUT", 25)) + 4:
                job["done"] = True
                job["err"] = job["err"] or "超时"
            else:
                # 云端一趟好几秒（板子上传慢），不显示秒数用户会以为卡死。
                # 只刷标题栏，别整个重绘 —— 每 0.15 秒一次的重绘太重。
                want = "云端识别中… %.0f 秒" % waited
                if want != self.hint_msg:
                    self.hint_msg = want
                    self.title.config(text=fit_text(want, W - PAD - 60,
                                                    size=TITLE_FONT))
                return

        self.cloud_job = None
        text = (job["text"] or "").strip()
        if text:
            self._apply_voice_text(text, "云端")
        else:
            # ⚠️ 这里要**现取**本地文本，不能用启动时存的那份：
            # 离线解码是在采集结束后另开线程跑的，启动云端那一刻它
            # 多半还没解完（job["local"] 是空串）。等云端回来（几秒后）
            # 它早就有结果了，现取才能顶得上。
            local = job["local"] or ""
            if not local and hasattr(self.asr, "text"):
                local = self.asr.text() or ""
            if job["err"]:
                self._apply_voice_text(local, "云端不可用，本地")
            else:
                self._apply_voice_text(local, "云端没听清，本地")

    def _apply_voice_text(self, text, why=""):
        """把识别结果填进输入框。**走 insert**，和键盘同一条路径。"""
        text = (text or "").strip()
        if text:
            self.buffer.insert(text)
            self.send_armed = False          # 内容变了，重新确认发送
            self.hint_msg = ("语音已填入（%s），可以改" % why) if why \
                else "语音已填入，可以改"
        else:
            # "没听到说话"和"说了但没听清"是两回事，提示要分开
            self.hint_msg = ("没听到说话" if why == "没听到说话"
                             else "没听清，再说一次")
        self._draw_input()

    def _poll_voice(self):
        """主循环里定期调：云端作业 + 录音中的中间结果。

        中间结果**不写进 buffer** —— 它还会变。只画在提示行上。
        """
        self._poll_cloud()
        if self.view != VIEW_INPUT or self.asr is None:
            return

        if self.voice_state == "finishing":
            if self.asr.busy():
                return                  # 解码线程还在退，下一轮再看
            self._finish_voice()
            return

        if not self.asr.busy():
            # 静音自动停：线程自己退了，但用户没点停止 ——
            # 以前这里直接 return，这段话就丢了，用户还得再点一次
            # （点下去又变成"开始录音"）。现在直接收尾。
            if self.voice_state == "recording":
                self._finish_voice()
            return

        part = self.asr.interim()
        want = "正在听：%s" % part if part else "正在听…"
        if want != self.hint_msg:
            self.hint_msg = want
            self.title.config(text=fit_text(want, W - PAD - 60,
                                            size=TITLE_FONT))

    # ---- 触摸 ----

    @staticmethod
    def _on_exit_btn(x, y):
        """点在右上角 ✕ 上了吗。

        ✕ 自己有 onclick 回调，但 canvas 的 Button-1 也会派发到
        handle_tap —— 它落在「右侧 1/3」里，于是点 ✕ 退出时还会顺手
        触发一次"往回翻 / 弹快捷回复"。退出是退出，别让它捎带动界面状态。
        """
        return x >= W - 34 and y <= 34

    def handle_tap(self, x, y):
        if self._on_exit_btn(x, y):
            return                  # 交给 ✕ 自己的回调，这里不重复处理
        if self.view == VIEW_INPUT:
            # 右上角的 ✕ **不在这里处理** —— 它自己有 onclick 回调
            # （_request_quit），unihiker 会先调它，再走这里。
            # 两边都写会重复触发，而且顺序不好保证。只留回调那一处。
            #
            # 点在候选栏上 → 选字 / 翻页
            if self.buffer.composing and self.buffer.candidates \
                    and CAND_TOP <= y < CAND_TOP + LINE_H:
                pages = getattr(self, "_cand_pages", 1)
                # ‹ › 箭头：翻页
                if pages > 1 and x < CAND_X0:
                    self.cand_page = max(0, self.cand_page - 1)
                    self._draw_input()
                    return
                if pages > 1 and x >= CAND_X0 + CAND_COUNT * CAND_W:
                    self.cand_page = min(pages - 1, self.cand_page + 1)
                    self._draw_input()
                    return
                # 候选格子：按**显示的位置**算下标，再加上翻页偏移
                i = int((x - CAND_X0) // CAND_W)
                if 0 <= i < CAND_COUNT:
                    real = self.cand_page * CAND_COUNT + i
                    if real < len(self.buffer.candidates) \
                            and self._pick_candidate(real):
                        # 内容变了 → 取消"待确认发送"。否则：点一下发送（变红）
                        # → 再把拼音选成字 → 再点一下就直接发出去了，
                        # 用户以为是第一次确认。
                        self.send_armed = False
                        self._draw_input()
                        return
            # 文本区点一下：把光标挪到点到的地方（大致的，按像素反推）
            if TEXT_TOP <= y < CAND_TOP:
                self._move_cursor_to(x, y)
                return
            if self._tap_key(x, y):
                return
            return

        if self.view in (VIEW_MENU, VIEW_REPLY):
            self._tap_box(x, y)
            return

        if self.view == VIEW_CHAT:
            if x < W // 3:
                self.scroll = min(self.scroll + BODY_LINES,
                                  max(0, len(self._cur_lines()) - BODY_LINES))
                self.show_chat()
            elif x > W * 2 // 3:
                if self.scroll > 0:
                    self.scroll = max(0, self.scroll - BODY_LINES)
                    self.show_chat()
                else:
                    self._open_replies()
            else:
                self._open_menu()
            return

        # 会话列表：点哪一行进哪个会话
        items = self.store.ordered()
        if not items:
            return
        per_page = 4
        start = (max(0, min(self.sel, len(items) - 1)) // per_page) * per_page
        row = (y - BODY_TOP) // (LINE_H * 2)
        idx = start + int(row)
        if 0 <= idx < len(items):
            self.sel = idx
            self.open_chat(idx)

    def _cur_lines(self):
        s = self.store.get(self.cur) if self.cur else None
        return message_lines(s["msgs"]) if s else []

    def _move_cursor_to(self, x, y):
        """点文本区把光标挪过去。

        按**像素**反推字符下标 —— 和画的时候用的是同一套宽度，
        所以点哪儿光标就去哪儿，不会偏。
        """
        avail = W - PAD * 2
        shown = self.buffer.display()
        lines, _r, _x = wrap_with_cursor(shown, len(shown), avail)
        if not lines:
            return
        # 屏幕上显示的是最后 TEXT_LINES 行，算一下点到的是哪一行
        offset = max(0, len(lines) - TEXT_LINES)
        row = int((y - TEXT_TOP) // LINE_H)
        idx_line = offset + max(0, min(row, TEXT_LINES - 1))
        if not (0 <= idx_line < len(lines)):
            return
        # 这一行开头在整串里的下标
        start = sum(len(lines[i]) for i in range(idx_line))
        # 行内按像素找最近的位置
        target = x - PAD
        acc = 0
        col = 0
        for ch in lines[idx_line]:
            w = text_width(ch)
            if acc + w // 2 >= target:
                break
            acc += w
            col += 1
        pos = start + col
        # 光标不能落到还没上屏的拼音里
        pos = min(pos, len(self.buffer.text))
        self.buffer.cursor = pos
        self._draw_input()

    def _tap_box(self, x, y):
        items = (MENU_ITEMS
                 if self.view == VIEW_MENU else config.QUICK_REPLIES)
        y0 = 80 if self.view == VIEW_MENU else 60
        bw, bh = 168, 26
        x0 = (W - bw) // 2
        if not (x0 <= x <= x0 + bw and y0 <= y <= y0 + bh * len(items) + 8):
            self._back_from_box()
            return
        idx = int((y - y0 - 6) // bh)
        if not (0 <= idx < len(items)):
            self._back_from_box()
            return

        if self.view == VIEW_REPLY:
            self._send(items[idx])
            return

        if idx == 0:
            self.open_input()
        elif idx == 1:
            self._open_replies()
        elif idx == 2:
            self.show_sessions()
        elif idx == 3:
            # 走 _make_client —— 演示模式下这里不能去连真服务器，
            # 否则点"断开重连"就成了偷偷登真号，把用户电脑上那个挤下线
            self.client.stop()
            time.sleep(0.3)
            self.client = self._make_client()
            self._wire_client()
            # ⚠️ Store 持有的是**上一个** client 的 groups/friends 字典
            # （构造时传的那个引用）。换成新 client 之后必须把引用也换
            # 过去，否则名字缓存和新 client 完全脱钩 —— 新拉回来的群名
            # 进不了 Store，会话名会一直卡在"群 123456"。
            # 重新建一个 Store 最省事，反正重连本来就是要从干净状态开始。
            self.store = Store(self.client.groups, self.client.friends)
            self.cur = None
            self.client.start()
            self.msg = "重连中…"
            self.show_sessions()
        else:
            self.quit_requested = True

    def _back_from_box(self):
        # 从输入界面弹出来的菜单，关掉要回输入界面（不是聊天页）——
        # 否则用户打字打一半弹个菜单，关掉就被踢回聊天页，输入全没了
        if self.view == VIEW_MENU and getattr(self, "menu_from_input", False):
            self.menu_from_input = False
            self.view = VIEW_INPUT
            self._hide_box()
            self._show_input_ui(True)
            self._draw_input()
            return
        self.menu_from_input = False
        self.show_chat()


# ---------------------------------------------------------------- 主循环

def main():
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    app = QQTerm()

    # ⚠️ 拼音索引在**启动时**就后台建，不要等用户打开键盘才建。
    # 建一次要十几秒（4 万条词 + 2 万字），以前是在 open_input 里启动，
    # 用户一按键就开始抢 CPU，打字明显发顿。放启动时建，用户还在看
    # 会话列表的时候就已经建好了。
    try:
        app._ensure_ime()
    except Exception:
        pass

    try:
        while True:
            if app.quit_requested:
                break

            # 收消息
            # 一次取走所有积压事件。按住 A/B 时键重复远快于
            # 一轮循环，以前每轮只处理一个，队列只会越积越长。
            events = app.drain()
            for ev in events:
                app.dispatch(ev)

            # 消息进 store，界面自动更新
            got = False
            while True:
                m = app.client.poll()
                if m is None:
                    break
                app.store.add(m)
                # 用户正开着这个会话在看 —— 不该算未读。
                # 否则看完一个群回列表，它自己头上顶着 "(1)"。
                if app.cur is not None and \
                        (m.kind, app.store.norm_sid(m.sid)) == app.cur:
                    app.store.mark_read(app.cur)
                got = True

            if got:
                # ⚠️ 必须按**当前视图**分派，不能只有 CHAT / else 两支。
                # 之前写成 `CHAT -> show_chat() else -> show_sessions()`，
                # 结果：打开"发送消息"浮层时 view 是 VIEW_REPLY（不是 CHAT），
                # 一来新消息就掉进 else → 弹回会话列表，浮层也没了。
                # 群里消息一来一回很快，表现就是"过一会儿刷新就退回主界面"。
                app.refresh_view()

            # 录音中：把中间结果画出来。放主循环里轮询，不用回调 ——
            # 回调是从录音线程来的，Tk 不是线程安全的。
            app._poll_voice()

            # 有事件就快点回来（按键跟手），闲的时候慢一点省电
            time.sleep(0.04 if events else 0.15)
    except KeyboardInterrupt:
        pass
    finally:
        app.client.stop()
        print("QQ 终端已退出")


if __name__ == "__main__":
    sys.exit(main())
