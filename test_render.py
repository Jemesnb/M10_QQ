#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消息渲染的离线测试 —— 不连网、不开界面，几秒跑完。

存在的理由：`message_lines` 里那个 TypeError 是在真机上靠肉眼发现的
（"一打开消息就报错"）。这种纯函数不该等到上板子才暴露。

跑法（本地或板子上都行）：
    python3 test_render.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

from app import message_lines, wrap_text, Store                        # noqa: E402
from app import clean, fit_text, text_width, set_measure          # noqa: E402
from app import wrap_text_px, TEXT_W, W as SCREEN_W               # noqa: E402
from app import FONT, TITLE_FONT                                  # noqa: E402
from textbuffer import TextBuffer                                 # noqa: E402
from onebot import (Event, extract_text, parse_event,             # noqa: E402
                    ReplyInfo, resolve_at, extract_text_raw,
                    at_token, has_at_token, AT_TOKEN_RE)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s   %s" % (name, detail))
        FAILED.append(name)


def ev(name, text, kind="group", sid=1, self_id=None):
    return Event(kind, sid, name, text, 1700000000, self_id)


def test_clean():
    """Tk 只认 BMP 字符，emoji 会让 canvas.itemconfigure 直接抛异常。

    这不是"显示成方块"的小问题 —— 是程序崩掉。实测第二条带 🍉 的消息
    就炸了整个终端：

        _tkinter.TclError: character U+1f349 is above the range
        (U+0000-U+FFFF) allowed by Tcl
    """
    print("\n[clean —— emoji 净化]")
    check("emoji 被替换", clean("🍉") == "□", repr(clean("🍉")))
    check("多个 emoji", clean("a🍉b😀c") == "a□b□c", repr(clean("a🍉b😀c")))
    check("中文不受影响", clean("你好世界") == "你好世界")
    check("英文数字不受影响", clean("abc123") == "abc123")
    check("标点不受影响", clean("，。！？~@#") == "，。！？~@#")
    check("换行保留", clean("a\nb") == "a\nb")
    check("零宽字符丢掉", clean("a​b") == "ab", repr(clean("a​b")))
    check("BOM 丢掉", clean("a﻿b") == "ab")
    check("私有区丢掉", clean("ab") == "ab")
    check("None 不炸", clean(None) == "")
    check("空串不炸", clean("") == "")

    # 关键验证：净化后的文本，每个字符都在 BMP 内。
    # 这条是真正的回归防线 —— 只要它过了，Tk 就不会因字符集崩。
    nasty = "正常文字🍉😀👍🏿🎉​﻿あ漢字１２３"
    cleaned = clean(nasty)
    bad = [(c, hex(ord(c))) for c in cleaned if ord(c) > 0xFFFF]
    check("净化后无辅助平面字符", not bad, repr(bad))


def test_fit_text():
    """按**像素**截断 —— 群名太长冲出屏幕的那个 bug。

    根因是原来用 `name[:13]`（13 个字符）。中文 17px、ASCII 9px，
    同样 13 个字符的像素宽度能差一倍，群名一长就冲出屏幕。
    """
    print("\n[fit_text —— 按像素截断]")

    # 离线时没有 Tk，用估算宽度（中文 17px / ASCII 9px）
    print("  （估算模式：中文 %dpx，ASCII %dpx）"
          % (text_width("中"), text_width("A")))

    # 短的不动
    check("短文本原样返回", fit_text("你好", 200) == "你好")

    # 超长的必须截到宽度以内
    long_cn = "第" + "一" * 40
    got = fit_text(long_cn, 200)
    check("超长中文被截断", text_width(got) <= 200,
          "%d px: %r" % (text_width(got), got))
    check("截断后加省略号", got.endswith("…"), repr(got))

    # ★ 中英混排：同样的"字符数"，像素宽度差一倍
    cn13 = "中" * 13
    en13 = "A" * 13
    check("（前提）13 个中文比 13 个 ASCII 宽得多",
          text_width(cn13) > text_width(en13) * 1.5,
          "%d vs %d" % (text_width(cn13), text_width(en13)))

    # 按字符数截会放过 en13（9*13=117px 没超），
    # 但如果可用宽只有 100px，按像素算就该被截
    mixed = "测试群(1)abcdefghij"
    got = fit_text(mixed, 100)
    check("混排按像素截到 100px 内", text_width(got) <= 100,
          "%d px: %r" % (text_width(got), got))

    # 极端窄：至少不炸，不返回超宽内容
    for w in (0, 1, 5, 8, 16, 17, 18):
        g = fit_text("测试群名称很长很长", w)
        check("宽度 %d 时不超宽" % w, text_width(g) <= w or g == "",
              "%d px -> %r" % (text_width(g), g))

    check("空串不炸", fit_text("", 100) == "")
    check("None 不炸", fit_text(None, 100) == "")

    # 真机用 Tk 测量时也要成立（这里模拟注入）
    real_calls = []

    def fake_measure(s):
        real_calls.append(s)
        return sum(20 if ord(c) > 0x2E7F else 10 for c in s)

    set_measure(fake_measure)
    try:
        got = fit_text("一二三四五六七八九十", 100)
        check("注入测量后按该测量截断",
              fake_measure(got) <= 100, "%d px: %r" % (fake_measure(got), got))
        check("确实调用了注入的测量函数", bool(real_calls))
    finally:
        set_measure(None)


def test_fit_text_by_size():
    """截断必须用**实际绘制字号**的宽度。

    真机实测：同一个字符串 12 号是 237px、14 号是 277px，差 17%。
    标题画的是 14 号，如果用 12 号的测量去截，算出来"装得下"，
    画出来就压到右边的 ✕ 上了（实测按 12 号截成 199px，14 号画出 224px）。
    """
    print("\n[按字号截断]")

    def make(sz):
        return lambda s: sum(
            (5 if c == " " else 17 if ord(c) > 0x2E7F else 11) * sz // 12
            for c in s)

    set_measure(make(FONT), FONT)
    set_measure(make(TITLE_FONT), TITLE_FONT)
    try:
        s = "NorthStarCraft 用户交流反馈群"
        check("两种字号的宽度确实不同",
              text_width(s, FONT) != text_width(s, TITLE_FONT),
              "%d vs %d" % (text_width(s, FONT), text_width(s, TITLE_FONT)))
        check("14 号更宽", text_width(s, TITLE_FONT) > text_width(s, FONT))

        for size in (FONT, TITLE_FONT):
            got = fit_text(s, 172, size=size)
            w = text_width(got, size)
            check("size=%d 截后不超宽" % size, w <= 172,
                  "%d px: %r" % (w, got))

        # 关键：用 12 号截出来的东西，14 号画会超宽 —— 所以 size 不能省
        wrong = fit_text(s, 172, size=FONT)
        check("（反向）省掉 size 就会超宽",
              text_width(wrong, TITLE_FONT) > 172,
              "12号截出 %d，14号画出来 %d"
              % (text_width(wrong, FONT), text_width(wrong, TITLE_FONT)))
    finally:
        set_measure(None, FONT)
        set_measure(None, TITLE_FONT)


def test_group_name_overflow():
    """真实的溢出场景：长群名 + 未读数后缀，不能超出屏宽。"""
    print("\n[群名溢出 —— 真机截图里发现的问题]")
    L = TEXT_W

    cases = [
        "测试群 (1)",
        "第1145141919号群",                      # 长数字
        "综合083-技术交流与资源分享群(新)",        # 长中文
        "一二三四五六七八九十" * 3,               # 超长
        "A群B群C群D群E群F群G群H群",
        "🎉活动群🎉",                            # emoji（净化后变方块）
    ]
    for name in cases:
        # 模拟 show_sessions 的调用（含未读数后缀）
        shown = fit_text(name + " (1)", L - 2)
        w = text_width(shown)
        check("「%s」不超宽" % name[:12], w <= L - 2, "%d px" % w)


def test_long_sender_name():
    """发送者名字很长时，正文不能被挤成"一行两个字"。

    真机截图里发现的：QQ 群名片二三十个字符很常见，prefix 直接用原名
    就有 300+px，比整行（224px）还宽。旧代码里 avail = max(40, 224-331)
    被兜成 40px —— 正文一行只放得下两个字，看着像一条竖线。
    """
    print("\n[超长发送者名字]")
    # 板子上实测：中文 17px、ASCII 11px、空格 5px（字号 12）
    real = []

    def m(s):
        real.append(s)
        w = 0
        for ch in s:
            if ch == " ":
                w += 5
            elif ord(ch) > 0x2E7F:
                w += 17
            else:
                w += 11
        return w

    set_measure(m)
    try:
        cases = [
            "第1145141919号超长群名片备用测试账号",
            "你完了 我今晚回去就把你的残骸全换成玄武岩😡",
            "[LRL5]葡萄无限枪战PVP服务器-玩家群",
            "NorthStarCraft 用户交流反馈群",
            "雨云RAINYUN.COM心灵驿站💘",
        ]
        body = "你好呀今天天气不错要不要一起出去走走"
        for name in cases:
            lines = message_lines([ev(name, body)], TEXT_W)
            over = [(t, text_width(t)) for t, _ in lines
                    if text_width(t) > TEXT_W]
            check("「%s…」每行不超宽" % name[:10], not over,
                  repr(over[:2]))

            # 正文第一行至少要放得下几个字，不能只剩一两个
            first = lines[0][0]
            body_part = first.split(": ", 1)[1] if ": " in first else first
            check("「%s…」正文没被挤成竖线" % name[:10],
                  text_width(body_part) >= 60,
                  "%d px: %r" % (text_width(body_part), body_part))

            # 正文内容不能丢
            joined = "".join(t.split(": ", 1)[-1] for t, _ in lines)
            check("「%s…」正文没丢字" % name[:10],
                  body in joined.replace(" ", "") or
                  body.replace(" ", "") in joined.replace(" ", ""),
                  repr(joined[:40]))
    finally:
        set_measure(None)


def test_wrap():
    print("\n[wrap_text]")
    check("空串不炸", wrap_text("", 10) == [""])
    check("短串原样", wrap_text("你好", 10) == ["你好"])
    long = "一" * 30
    out = wrap_text(long, 10)
    check("长串按宽度折", all(len(x) <= 10 for x in out), repr(out))
    check("折行不丢字", "".join(out) == long)
    check("换行符切段", wrap_text("a\nb", 10) == ["a", "b"])


def test_reply_cache_invalidated():
    print("\n[引用异步补全]")

    class FakeReply(object):
        """照 onebot.ReplyInfo 的字段写（它有 __slots__，不能用假 dict）。"""
        def __init__(self):
            self.message_id = "1"
            self.text = ""
            self.sender = ""
            self.sender_id = None
            self.done = False

        def label(self):
            if self.text:
                return (self.sender + ": " + self.text) if self.sender \
                    else self.text
            return "引用了消息"

    e = ev("张三", "回复你")
    e.reply = FakeReply()
    first = message_lines([e], TEXT_W)
    check("引用没到显示占位", "引用了消息" in first[0][0], repr(first[0][0]))

    # 模拟 _fill_reply：补到内容 + 清缓存
    e.reply.text = "原来那条消息"
    e.reply.sender = "李四"
    e._lines = None
    second = message_lines([e], TEXT_W)
    check("引用到了能显示出来",
          "原来那条消息" in second[0][0], repr(second[0][0]))
    check("★ 不清缓存就永远显示占位", first != second)

def test_audioop_fallback():
    print("\n[audioop 兜底（Python 3.13 移除）]")
    import voice
    import array

    # 16bit 立体声：L=1000, R=-1000
    fr = array.array("h", [1000, -1000, 1000, -1000])
    if hasattr(fr, "tobytes"):
        raw = fr.tobytes()
    else:
        raw = fr.tostring()
    left = voice.left_channel(raw)
    got = array.array("h")
    got.frombytes(left)
    check("取左声道", list(got) == [1000, 1000], repr(list(got)))

    # RMS 应该等于 1000（全 1000 的样本）
    check("RMS 算得对", voice.rms16(left) == 1000, str(voice.rms16(left)))
    check("空片段 RMS 为 0", voice.rms16(b"") == 0)

def test_voice_edge_cases():
    print("\n[语音边界 —— 失败路径不能卡死界面]")
    import app as appmod

    # 1) 引擎起不来时，busy() 必须回到 False（否则界面卡在"结束"）
    from voice import VoskEngine
    eng = VoskEngine("/nonexistent/model")
    eng._capturing = True                 # 假装正在录
    eng._stream = None
    eng._model = None
    eng._run()                            # 会走"开不了设备"的提前返回
    check("★ 提前返回也清掉 capturing", eng.busy() is False, str(eng.busy()))

    # 2) 没录到音频就别上传云端（不再做静音检测，判据是"有没有音频"）
    class NoAudioAsr(object):
        def text(self):
            return ""

        def wav(self):
            return b""                      # 引擎没导出任何音频

    class Cloud(object):
        def __init__(self):
            self.calls = 0

        def transcribe(self, wav):
            self.calls += 1
            return "不该被调用"

    t = appmod.QQTerm.__new__(appmod.QQTerm)
    t.asr = NoAudioAsr()
    t.cloud = Cloud()
    t.cloud_job = None
    t.voice_state = "finishing"
    t.view = appmod.VIEW_INPUT
    t.hint_msg = ""
    t.buffer = appmod.TextBuffer()
    t.send_armed = False
    t._draw_input = lambda: None
    appmod.QQTerm._finish_voice(t)
    check("★ 没有音频不上传", t.cloud.calls == 0, "%d 次" % t.cloud.calls)
    check("回 idle", t.voice_state == "idle", t.voice_state)
    check("提示写清楚", "没听到说话" in t.hint_msg, t.hint_msg)

    # 3) 云端失败时现取本地文本（离线解码可能是稍后才完成的）
    class LateAsr(object):
        def __init__(self):
            self.calls = 0

        def text(self):
            self.calls += 1
            return "本地结果" if self.calls > 1 else ""

    t2 = appmod.QQTerm.__new__(appmod.QQTerm)
    t2.asr = LateAsr()
    t2.asr.calls = 1                 # 第一次取是空（_finish_voice 时）
    t2.cloud = None
    t2.voice_state = "idle"
    t2.view = appmod.VIEW_INPUT
    t2.hint_msg = ""
    t2.buffer = appmod.TextBuffer()
    t2.send_armed = False
    t2._draw_input = lambda: None
    t2.cloud_job = {"done": True, "text": "", "err": "HTTP 500",
                    "local": "", "t0": 0}
    appmod.QQTerm._poll_cloud(t2)
    check("★ 云端失败时现取本地文本", t2.buffer.text == "本地结果",
          repr(t2.buffer.text))


def test_voice_state_machine():
    print("\n[语音状态机 —— 点停止不能卡住界面]")
    import app as appmod

    class FakeAsr(object):
        def __init__(self):
            self.starts = 0
            self.stops = 0
            self.running = False

        def available(self):
            return True

        def reason(self):
            return ""

        def busy(self):
            return self.running

        def start(self):
            self.starts += 1
            self.running = True
            return True

        def request_stop(self):
            # 真实引擎里只是设标志位，立刻返回
            self.stops += 1
            self.running = False

        def text(self):
            return "本地结果"

        def wav(self):
            return b"RIFF" + b"\0" * 64   # 有音频（云端为 None → 走本地）

        def interim(self):
            return ""

        def error(self):
            return ""

    t = appmod.QQTerm.__new__(appmod.QQTerm)
    t.asr = FakeAsr()
    t.cloud = None
    t.cloud_job = None
    t.voice_state = "idle"
    t.view = appmod.VIEW_INPUT
    t.hint_msg = ""
    t.buffer = appmod.TextBuffer()
    t.send_armed = False
    t._draw_input = lambda: None

    appmod.QQTerm._toggle_voice(t)                 # 开始录
    check("第一下开始录音", t.voice_state == "recording", t.voice_state)

    appmod.QQTerm._toggle_voice(t)                 # 请求停止
    check("★ 停止是立刻返回的（不 join）", t.asr.stops == 1)
    check("进入 finishing", t.voice_state == "finishing", t.voice_state)

    # 用户着急又点了一下 —— 不能变成"重新开始录音"
    appmod.QQTerm._toggle_voice(t)
    check("★ 收尾中再点一下被忽略", t.asr.starts == 1, "%d 次" % t.asr.starts)

    appmod.QQTerm._poll_voice(t)                   # 收尾完成
    check("回 idle", t.voice_state == "idle", t.voice_state)
    check("本地结果进了输入框", t.buffer.text == "本地结果", repr(t.buffer.text))

    # 引擎自己退了（比如录到 MAX_REC_SEC 上限），用户没点停止 —— 也得收尾
    t.buffer.clear()
    appmod.QQTerm._toggle_voice(t)
    t.asr.running = False                          # 引擎自己退出了
    appmod.QQTerm._poll_voice(t)
    check("★ 引擎自己停了也会收尾（比如到上限）", t.buffer.text == "本地结果", repr(t.buffer.text))


def test_cand_width():
    print("\n[候选栏宽度 —— 两个字的词不能被截成省略号]")
    import app as appmod

    # 用户看到一排「…」，其实是格子 33px < 两字 34px，被 fit_text 截了
    for w in ("测试", "百度", "你好", "中国"):
        shown = appmod.fit_text(w, appmod.CAND_W - 2)
        check("%s 完整显示" % w, shown == w, repr(shown))

    need = appmod.text_width("测试")
    check("格子放得下两个字", appmod.CAND_W - 2 >= need,
          "需要 %d, 可用 %d" % (need, appmod.CAND_W - 2))

    # 候选区 + 右箭头不能撞
    right = appmod.CAND_X0 + appmod.CAND_COUNT * appmod.CAND_W
    check("候选区不压到 ›", right <= appmod.W - 14, "%d > %d" % (right, appmod.W - 14))
    check("左边给 ‹ 留了位置", appmod.CAND_X0 >= 14, str(appmod.CAND_X0))


def test_keyboard_pages():
    print("\n[键盘四态 —— 中 / 英 / 中符 / 英符]")
    import app as appmod

    t = appmod.QQTerm.__new__(appmod.QQTerm)
    t.g = FakeGUI()
    t.title = FakeItem("text", text="")
    t.hint_msg = ""
    t.kb_shift = False
    t.kb_mode = "cn"
    t.asr = None
    t.send_armed = False
    t.buffer = appmod.TextBuffer()
    appmod.QQTerm._build_input_ui(t, t.g)

    modes = appmod.KB_MODES
    check("四态顺序固定", list(modes) == ["cn", "en", "cn_sym", "en_sym"],
          str(list(modes)))

    # 1) 四页几何必须完全一致，且每行正好铺满 240（行2 两侧各 12 缩进）
    shapes = set()
    for m in modes:
        pg = appmod.KB_PAGES[m]
        shapes.add(tuple(len(r) for r in pg))
        for idx, row in enumerate(pg):
            width = sum(k[1] for k in row) + (24 if idx == 1 else 0)
            check("%s 第%d行铺满" % (m, idx + 1), width == 240, "%dpx" % width)
    check("四页键数完全一致", len(shapes) == 1, str(shapes))

    # 2) 切页不能让键位移动
    before = [k[0].pos for k in t.keys]
    for m in modes + ("cn",):
        t.kb_mode = m
        appmod.QQTerm._refresh_keys(t)
        check("%s 切换后键位没动" % m, [k[0].pos for k in t.keys] == before)

    # 3) 键帽内容
    t.kb_mode = "cn_sym"
    appmod.QQTerm._refresh_keys(t)
    labels = [k[3] for k in t.keys]
    check("中符页第一行是数字",
          labels[:10] == list("1234567890"), repr(labels[:10]))
    check("中符页有中文逗号", "，" in labels)
    t.kb_mode = "en_sym"
    appmod.QQTerm._refresh_keys(t)
    labels = [k[3] for k in t.keys]
    check("英符页有 @", "@" in labels, repr(labels[:12]))
    check("英符页没有中文标点", "，" not in labels)

    # 4) 每个键点下去解出来的动作都对
    for m in modes:
        t.kb_mode = m
        appmod.QQTerm._refresh_keys(t)
        hits = []
        t._do_key = lambda a: hits.append(a)
        ok_all = True
        for r, row in enumerate(appmod.KB_PAGES[m]):
            x = appmod.KB_ROW2_INDENT if r == 1 else 0
            y = appmod.KB_TOP + r * appmod.KB_ROW_H + 10
            for label, kwidth, action in row:
                hits[:] = []
                if not appmod.QQTerm._tap_key(t, x + kwidth // 2, y) \
                        or hits[:1] != [action]:
                    check("%s 页 %r 键解码" % (m, label), False,
                          "点到 %r" % (hits[:1],))
                    ok_all = False
                    break
                x += kwidth
        check("%s 页全部键解码正确" % m, ok_all)
        del t._do_key

    # 5) 模式键：四态循环 + 键帽显示当前状态
    t._draw_input = lambda: None
    t.kb_mode = "cn"
    for want in ("en", "cn_sym", "en_sym", "cn"):
        appmod.QQTerm._do_key(t, ("mode", None))
        check("循环到 %s" % want, t.kb_mode == want, t.kb_mode)
        appmod.QQTerm._refresh_keys(t)
        # 看控件上的**实际文字**（unihiker 把 config 的值存在 .kw 里）
        shown = [k[1].kw.get("text") for k in t.keys if k[2][0] == "mode"]
        check("键帽显示当前状态 %r" % appmod.KB_MODE_LABEL[want],
              shown == [appmod.KB_MODE_LABEL[want]], repr(shown))


def test_drain_keeps_identity():
    print("\n[drain —— 不能换掉列表对象]")
    from app import QQTerm

    class Holder(object):
        pass

    o = Holder()
    o.pending = []
    # 模拟 _wire_client：回调**抓住列表对象**
    held = o.pending
    o.pending.append("a")

    got = QQTerm.drain(o)
    check("drain 取走已有事件", got == ["a"], repr(got))

    # 回调往它抓的那个列表塞 —— 必须还能被 drain 看到
    held.append(("groups", None))
    again = QQTerm.drain(o)
    check("★ drain 之后回调仍然有效", again == [("groups", None)],
          "取到 %r（空说明列表对象被换掉了）" % (again,))
    check("列表对象身份没变", o.pending is held)

def test_clean_surrogate():
    print("\n[clean —— 孤立代理字符]")
    lone = chr(0xD83D)          # 服务器把代理对发断，只剩半个
    emoji = chr(0x1F600)        # 正常的 emoji（一个 >0xFFFF 的字符）

    check("孤立代理换方块", clean("a" + lone + "文") == "a□文",
          repr(clean("a" + lone + "文")))
    check("残留代理为零",
          not any(0xD800 <= ord(c) <= 0xDFFF for c in clean(lone * 5)))
    check("正常 emoji 也换方块", clean(emoji) == "□", repr(clean(emoji)))
    check("普通文字不动", clean("你好 abc") == "你好 abc")

def test_sid_normalized():
    print("\n[Store —— sid 类型归一化]")
    from app import Store
    from onebot import Event
    st = Store()
    # 消息里 sid 是数字
    st.add(Event("group", 894859599, "张三", "hi", 1700000000, None))
    check("数字 sid 建了会话", len(st.sessions) == 1, str(len(st.sessions)))
    # 名单里是字符串（get_group_list 存进来时写了 str）
    st.ensure("group", "894859599", "同一个群")
    check("★ 字符串 sid 不新建会话", len(st.sessions) == 1,
          "%d 个: %s" % (len(st.sessions), list(st.sessions)))
    st.add(Event("group", "894859599", "张三", "又来一条", 1700000001, None))
    check("★ 字符串 sid 的消息也不新建", len(st.sessions) == 1,
          "%d 个" % len(st.sessions))
    check("消息都进了同一个会话",
          len(st.sessions[("group", 894859599)]["msgs"]) == 2,
          str(len(st.sessions[("group", 894859599)]["msgs"])))

def test_exit_btn_guard():
    print("\n[点 ✕ 不能触发别的动作]")
    from app import QQTerm
    check("✕ 在判定区内", QQTerm._on_exit_btn(240 - 20, 10))
    check("✕ 边界外不算", not QQTerm._on_exit_btn(240 - 60, 10))
    check("正文区不算", not QQTerm._on_exit_btn(200, 200))

def test_line_cache():
    print("\n[折行缓存]")
    import app as appmod

    saved = dict(appmod._measure_by_size)
    saved_m = appmod._measure
    calls = [0]

    def counting(s):
        calls[0] += 1
        w = 0
        for ch in s:
            w += 17 if ord(ch) > 0x2E7F else 9
        return w

    appmod.set_measure(counting, appmod.FONT)
    try:
        e = ev("张三", "今天天气不错，我们出去走走吧")
        l1 = message_lines([e], TEXT_W)
        n1 = calls[0]
        l2 = message_lines([e], TEXT_W)
        n2 = calls[0]
        check("第二次折行不再问字宽", n2 == n1, "%d -> %d 次" % (n1, n2))
        check("两次结果一致", l1 == l2)
        check("缓存挂在消息对象上", e._lines is not None)
        check("按宽度分别缓存", e._lines_w == TEXT_W)

        # 文本改了要重新折（refresh_at_names 里会把缓存清掉）
        e.text = "换个内容"
        e._lines = None
        l3 = message_lines([e], TEXT_W)
        check("文本改了会重新折",
              any("换个内容" in t for t, _ in l3), repr(l3[:1]))

        # 字宽缓存：同一个字只量一次
        appmod._w_cache.clear()
        calls[0] = 0
        for _ in range(50):
            text_width("的")
        check("同一字符只量一次", calls[0] == 1, "%d 次" % calls[0])

        # 换测量函数必须让缓存失效，否则换字体后宽度全错
        appmod.set_measure(counting, appmod.FONT)
        check("set_measure 会清字宽缓存", len(appmod._w_cache) == 0)
    finally:
        appmod._measure_by_size.clear()
        appmod._measure_by_size.update(saved)
        appmod._measure = saved_m
        appmod._w_cache.clear()


def test_message_lines():
    print("\n[message_lines]")
    # 这就是当初炸掉的那个调用路径：真机上一点进会话就 TypeError
    try:
        lines = message_lines([ev("张三", "你好啊")], TEXT_W)
        check("单条消息不抛异常", True)
    except TypeError as exc:
        check("单条消息不抛异常", False, "TypeError: %s" % exc)
        return

    check("返回 (文本, 是否自己) 二元组",
          all(isinstance(x, tuple) and len(x) == 2 for x in lines),
          repr(lines[:2]))
    check("第二项是 bool",
          all(isinstance(x[1], bool) for x in lines))
    check("带发送者前缀", lines[0][0].startswith("张三:"), repr(lines[0][0]))

    # 自己发的消息标记成 SELF 颜色
    lines = message_lines([ev("我", "我发的")], TEXT_W)
    check("自己的消息 is_self=True", lines[0][1] is True)

    lines = message_lines([ev("张三", "别人发的")], TEXT_W)
    check("别人的消息 is_self=False", lines[0][1] is False)

    # 超长消息折行：每条消息至少产出一行，且不超宽
    many = [ev("张三", "很长的消息" * 20)]
    lines = message_lines(many, TEXT_W)
    check("长消息折成多行", len(lines) > 1, "%d 行" % len(lines))
    check("每行不超宽（按像素）",
          all(text_width(t) <= TEXT_W for t, _ in lines),
          repr([(t, text_width(t)) for t, _ in lines
                if text_width(t) > TEXT_W][:3]))

    # 折行后的续行要有缩进，和首行对齐
    cont = [t for t, _ in lines[1:] if t.strip()]
    check("续行有缩进", all(t.startswith(" ") for t in cont),
          repr(cont[:2]))

    # 空文本不能炸
    try:
        message_lines([ev("张三", "")], TEXT_W)
        check("空文本不炸", True)
    except Exception as exc:
        check("空文本不炸", False, repr(exc))


def test_store():
    print("\n[Store]")
    from app import config
    s = Store()
    s.add(ev("张三", "第一条", sid=100))
    s.add(ev("张三", "第二条", sid=100))
    s.add(ev("李四", "别的群", sid=200))
    check("两个会话", len(s.ordered()) == 2)
    check("最近活跃排前面", s.order[0][1] == 200, repr(s.order))

    s.add(ev("张三", "第三条", sid=100))
    check("新消息把会话顶到最前", s.order[0][1] == 100, repr(s.order))
    check("消息没丢", len(s.get(("group", 100))["msgs"]) == 3)

    # 自己发的消息
    s.add_self(("group", 100), "我发的")
    check("自己发的进列表", len(s.get(("group", 100))["msgs"]) == 4)

    # ⚠️ 给**不存在**的会话 add_self —— 以前是直接 return（静默丢掉），
    # 那条路现在故意改了：消息既然发出去了，界面上就得有，
    # 否则用户以为没发出去又发一遍。所以现在会自动建出会话。
    n_before = len(s.sessions)
    s.add_self(("group", 999), "没有这个会话")
    got = s.get(("group", 999))
    check("★ add_self 对未知会话会自动建出来（不能静默丢）",
          got is not None and len(got["msgs"]) == 1,
          repr(got))
    check("会话数确实增加了", len(s.sessions) == n_before + 1,
          "%d -> %d" % (n_before, len(s.sessions)))

    # MAX_MESSAGES 截断
    s2 = Store()
    for i in range(config.MAX_MESSAGES + 50):
        s2.add(ev("张三", "msg%d" % i, sid=1))
    n = len(s2.get(("group", 1))["msgs"])
    check("超出上限被截断", n == config.MAX_MESSAGES, "%d 条" % n)


def test_extract_text():
    print("\n[extract_text]")
    check("字符串直接返回", extract_text("hi", "hi") == "hi")
    segs = [{"type": "text", "data": {"text": "你好"}},
            {"type": "image", "data": {}},
            {"type": "face", "data": {}}]
    check("图片表情占位", extract_text(segs, "") == "你好[图片][表情]",
          repr(extract_text(segs, "")))
    check("空数组回退 raw_message", extract_text([], "raw") == "raw")
    check("None 不炸", extract_text(None, "raw") == "raw")


def test_parse_event():
    print("\n[parse_event]")
    check("非消息事件返回 None",
          parse_event({"post_type": "meta_event"}) is None)
    check("非 dict 返回 None", parse_event("garbage") is None)

    payload = {"post_type": "message", "message_type": "group",
               "group_id": 123, "user_id": 456,
               "sender": {"nickname": "张三", "card": "群名片"},
               "raw_message": "你好", "time": 1700000000, "self_id": 999}
    e = parse_event(payload)
    check("群消息 kind", e.kind == "group")
    check("群消息 sid 是 group_id", e.sid == 123)
    check("优先用群名片", e.name == "群名片", e.name)

    payload["sender"] = {"nickname": "张三"}
    e = parse_event(payload)
    check("没名片用昵称", e.name == "张三", e.name)

    # 自己发的消息要标成"我"
    payload["user_id"] = 999
    payload["sender"] = {"nickname": "张三"}
    e = parse_event(payload, self_id=999)
    check("自己发的标成'我'", e.name == "我", e.name)
    check("自己发的 is_self()", e.is_self() is True)

    # 私聊用 user_id 当 sid
    p2 = {"post_type": "message", "message_type": "private",
          "user_id": 456, "sender": {"nickname": "李四"},
          "raw_message": "在吗"}
    e = parse_event(p2)
    check("私聊 kind", e.kind == "private")
    check("私聊 sid 是 user_id", e.sid == 456)


def test_end_to_end_no_nonbmp():
    """端到端：带 emoji 的消息走完整条链路，出来必须仍全是 BMP。

    这条测试对应真机上那个崩溃 —— 只测 clean() 不够，因为可能
    有某条路径绕过了净化（比如昵称、群名、预览）。这里模拟真实
    数据（emoji 昵称 + emoji 正文 + emoji 群名）走一遍 Store →
    message_lines，把**所有**会进 Tk 的文本都检查一遍。
    """
    print("\n[端到端 —— 没有非 BMP 字符能溜到 Tk]")
    s = Store()

    # 昵称带 emoji、正文带 emoji、还夹着零宽字符
    s.add(ev("🍉卖瓜的🍉", "今天西瓜😀很甜​哦", sid=100))
    s.add(ev("A👍B", "收到🎉", sid=100))
    s.add(ev("普通昵称", "普通消息", sid=200))
    s.add_self(("group", 100), "我发的也带😀")

    # 把所有会进 Tk 的文本收集起来，逐一检查
    texts = []
    for key, sess in s.ordered():
        texts.append(sess["name"])                    # 会话标题 / 列表名
        last = sess["msgs"][-1]
        texts.append(last.name)                       # 预览里的发送者
        texts.append(last.text)                       # 预览里的正文
        for line, _is_self in message_lines(sess["msgs"], TEXT_W):
            texts.append(line)                        # 聊天页每一行

    bad = []
    for t in texts:
        for c in str(t):
            if ord(c) > 0xFFFF:
                bad.append((t[:30], repr(c), hex(ord(c))))

    check("会话名/预览/正文全部可显示", not bad, repr(bad[:3]))
    check("emoji 已变成方块", any("□" in str(t) for t in texts),
          "没找到替换痕迹，可能净化没生效")

    # 反向验证：如果**不**净化，确实会产生非 BMP 字符
    raw = ev("🍉", "🍉", sid=1)
    check("（反向）未净化的原文确实含非 BMP",
          any(ord(c) > 0xFFFF for c in raw.name + raw.text),
          "样例数据不够刁钻")


def test_group_names():
    """群名 —— 群里来的消息只带 group_id，不带群名。

    参考 C:\\Users\\19565\\Desktop\\python\\qq：那边是启动拉 get_group_list
    建 groupsCache，前端 nameOf() 去查。这边同理，而且群名是**异步**到的
    （连上后才拉），消息可能先到，所以会话名要能后补。
    """
    print("\n[群名]")

    class FakeClient(object):
        """只提供 lookup_group 的替身。"""
        def __init__(self, names):
            self._n = names

        def lookup_group(self, gid):
            return self._n.get(str(gid))

    st = Store()
    st.add(ev("张三", "还没拉到群名时的消息", sid=894859599))
    check("群名未知时用号码占位",
          st.ordered()[0][1]["name"] == "群 894859599",
          st.ordered()[0][1]["name"])

    # 群名到了 → 后补
    st.refresh_names(FakeClient({"894859599": "测试群"}).lookup_group)
    check("群名到了能补上",
          st.ordered()[0][1]["name"] == "测试群",
          st.ordered()[0][1]["name"])

    # 未知群号必须**不覆盖** —— 否则占位名会把已有名字盖掉
    st.refresh_names(FakeClient({}).lookup_group)
    check("查不到时保持原样（不被占位覆盖）",
          st.ordered()[0][1]["name"] == "测试群",
          st.ordered()[0][1]["name"])

    # 群名带 emoji 也要净化
    st2 = Store()
    st2.add(ev("张三", "x", sid=111))
    st2.refresh_names(FakeClient({"111": "测试群🍉"}).lookup_group)
    name = st2.ordered()[0][1]["name"]
    check("群名里的 emoji 被净化",
          all(ord(c) <= 0xFFFF for c in name), repr(name))
    check("群名净化后是测试群□", name == "测试群□", repr(name))

    # 私聊的会话名不该被群名刷新碰到
    st.add(ev("李四", "私聊", kind="private", sid=456))
    before = [s["name"] for k, s in st.ordered() if k[0] == "private"]
    st.refresh_names(FakeClient({"894859599": "改了"}).lookup_group)
    after = [s["name"] for k, s in st.ordered() if k[0] == "private"]
    check("私聊会话名不受影响", before == after, "%r vs %r" % (before, after))


def test_reply():
    """引用消息 —— 参考 QQ web 项目的 _extract_reply / renderReplyToken。

    要点：拿不到被引用内容时，**不能留空白**，要显示"引用了消息"。
    """
    print("\n[引用消息]")

    # 1) 有完整的引用内容
    e = ev("张三", "同意", sid=1)
    e.reply = ReplyInfo("12345")
    e.reply.text = "明天开会吗"
    e.reply.sender = "李四"
    lines = message_lines([e], TEXT_W)
    check("引用占一行且带发送者",
          any(l[0].startswith("↩ 李四: 明天开会吗") for l in lines),
          repr([l[0] for l in lines]))
    check("引用行下面是正文",
          any("张三:" in l[0] for l in lines), repr([l[0] for l in lines]))

    # 2) 只有文本，没发送者
    e = ev("张三", "同意", sid=1)
    e.reply = ReplyInfo("1")
    e.reply.text = "明天开会吗"
    q = [l[0] for l in message_lines([e], TEXT_W) if l[0].startswith("↩")]
    check("没发送者时不出现空冒号", q and ": " not in q[0], repr(q))

    # 3) 只有发送者，没文本
    e = ev("张三", "同意", sid=1)
    e.reply = ReplyInfo("1")
    e.reply.sender = "李四"
    q = [l[0] for l in message_lines([e], TEXT_W) if l[0].startswith("↩")]
    check("没文本时显示'李四 的消息'", q and "李四" in q[0], repr(q))

    # 4) ★ 两个都没拿到 —— 最关键的一条
    e = ev("张三", "同意", sid=1)
    e.reply = ReplyInfo("1")
    lines = message_lines([e], TEXT_W)
    q = [l[0] for l in lines if l[0].startswith("↩")]
    check("全拿不到时也有占位，不留空白",
          q and "引用了消息" in q[0], repr(q))

    # 5) 没有引用时不该多出引用行
    lines = message_lines([ev("张三", "普通消息", sid=1)], TEXT_W)
    check("没引用时不产生引用行",
          not any(l[0].startswith("↩") for l in lines),
          repr([l[0] for l in lines]))

    # 6) 引用内容超宽要截断
    e = ev("张三", "同意", sid=1)
    e.reply = ReplyInfo("1")
    e.reply.text = "很长的引用内容" * 20
    q = [l[0] for l in message_lines([e], TEXT_W) if l[0].startswith("↩")]
    check("超宽引用被截断", q and text_width(q[0]) <= TEXT_W,
          "%d px" % (text_width(q[0]) if q else -1))

    # 7) 引用里带 emoji
    e = ev("张三", "同意", sid=1)
    e.reply = ReplyInfo("1")
    e.reply.text = "好的🍉"
    e.reply.sender = "李四"
    st = Store()
    st.add(e)     # Store.add 负责净化
    lines = message_lines(st.ordered()[0][1]["msgs"], TEXT_W)
    flat = "".join(t for t, _ in lines)
    check("引用里的 emoji 被净化",
          all(ord(c) <= 0xFFFF for c in flat), repr(flat))


def test_at_names():
    """@ 必须显示群名片，**不能显示 QQ 号**。

    用户原话："不要显示QQ号 显示那个人的群名称"。参考
    C:\\Users\\19565\\Desktop\\python\\qq 的 feedAtName/atNameMap。

    难点在于名字是**异步**到的：at 段只有号码，名字要么在成员表里
    （每条群消息攒的），要么得现查 API。所以正文里先埋占位符，
    名字到了再回填。这里把三层都测一遍。
    """
    print("\n[@ 显示名 —— 不露 QQ 号]")

    # ---- 占位符本身 ----
    check("占位符能被认出来", has_at_token(at_token(0)))
    check("普通文本不算占位符", not has_at_token("你好@张三"))
    check("占位符认得出序号",
          AT_TOKEN_RE.findall(at_token(0) + at_token(1)) == ["0", "1"])
    # ★ 占位符里**不能有 QQ 号**。存号码的话，哪天漏了一处 resolve_at
    # 就在屏幕上摊出一串数字；存序号最坏也不过是显示 ⟨at:0⟩。
    check("占位符里没有 QQ 号", "123456" not in at_token(0))

    # ---- extract_text_raw：@ 只出占位符，不出号码 ----
    segs = [{"type": "at", "data": {"qq": "123456"}},
            {"type": "text", "data": {"text": " 你好"}}]
    raw, ids = extract_text_raw(segs, "")
    check("@ 提取出 QQ 号列表", ids == ["123456"], repr(ids))
    # ★ 核心：加工过的文本里**不能出现号码**
    check("原始文本里没有裸露的 QQ 号", "123456" not in raw, repr(raw))
    check("原始文本里是占位符", at_token(0) in raw, repr(raw))
    # QQ 的 raw_message 是 "[CQ:at,qq=x] 你好"，@ 后面已经补了空格，
    # 正文再来一个就是两个空格 —— 白占 5px，小屏上看得见
    check("不出现双空格", "  " not in raw, repr(raw))

    raw2, ids2 = extract_text_raw(
        [{"type": "at", "data": {"qq": "all"}},
         {"type": "text", "data": {"text": " 开会"}}], "")
    check("@全体成员直接定死", "@所有人" in raw2 and not ids2, repr((raw2, ids2)))

    raw3, ids3 = extract_text_raw([{"type": "at", "data": {}}], "")
    check("at 段没有 qq 时不崩", ids3 == [], repr((raw3, ids3)))

    # ---- 解析：查得到名字 ----
    names = {"123456": "张三"}
    out = resolve_at(at_token(0) + " 你好", ["123456"], names)
    check("查得到就显示群名片", out == "@张三 你好", repr(out))
    check("解析后的文本里没有 QQ 号", "123456" not in out, repr(out))

    # ---- 解析：查不到时**也不能露号码** ----
    out2 = resolve_at(at_token(0) + " 你好", ["999"], names)
    check("查不到退成 @…", "@…" in out2, repr(out2))
    # ★ 这是用户明确要求的：宁可不显示，也不显示号码
    check("查不到时不露 QQ 号", "999" not in out2, repr(out2))

    # 一条消息 @ 两个人，各解析各的
    # （用同一个 "@…" 当占位符就会串 —— 这就是占位符要带序号的原因）
    two = at_token(0) + " " + at_token(1) + " 来一下"
    out3 = resolve_at(two, ["111", "222"], {"111": "张三", "222": "李四"})
    check("两个人都解析对", out3 == "@张三 @李四 来一下", repr(out3))
    out4 = resolve_at(two, ["111", "222"], {"111": "张三"})
    check("只有一个人有名时另一个退成 @…",
          out4 == "@张三 @… 来一下", repr(out4))
    # 序号越界（不该发生，但坏数据不能崩）
    check("序号越界退成 @… 不崩",
          resolve_at(at_token(9), ["111"], {"111": "张三"}) == "@…",
          repr(resolve_at(at_token(9), ["111"], {"111": "张三"})))

    # ---- 群号必须参与查询：不同群里同一个 QQ 号是两个人 ----
    from onebot import make_lookup
    two_groups = {("100", "7"): "甲群的小明", ("200", "7"): "乙群的老王"}
    lk1, lk2 = make_lookup(two_groups, 100), make_lookup(two_groups, 200)
    check("同号不同群查出来不同", lk1("7") == "甲群的小明"
          and lk2("7") == "乙群的老王", repr((lk1("7"), lk2("7"))))
    check("没这个人的群查不到", make_lookup(two_groups, 300)("7") is None)
    check("裸表也能查（不带群号）",
          make_lookup({"7": "小明"}, 100)("7") == "小明")
    check("空表查不到任何东西", make_lookup(None, 100)("7") is None)

    # ---- 成员表 ----
    from onebot import OneBotClient
    c = OneBotClient("127.0.0.1", 1)
    check("表里没人时查不到", c.member_name(1, 2) is None)
    # 哪些值得去查：表里有答案的不查，别白跑 API
    check("表里没有的人要查", c.missing_names(1, ["2"]) == ["2"])
    check("空列表要查什么", c.missing_names(1, []) == [])
    check("None 不炸", c.missing_names(1, None) == [])
    check("喂进去能查出来",
          c.feed_member(1, 2, "张三") and c.member_name(1, 2) == "张三")
    check("表里有了就不用查了", c.missing_names(1, ["2"]) == [])
    # 退群的人查了也查不到，只问一次 —— 否则每被 @ 一次就白跑一趟
    c._dead_names.add(("1", "404"))
    check("查失败过的不再查", c.missing_names(1, ["404"]) == [])
    check("喂 None 不炸", c.feed_member(1, None, "张三") is False)
    check("喂空名字不炸", c.feed_member(1, 2, "") is False)
    check("重名不算变更", c.feed_member(1, 2, "张三") is False)
    check("改名算变更", c.feed_member(1, 2, "张三丰") is True)
    check("不同群同名互不干扰",
          c.member_name(1, 2) == "张三丰" and c.member_name(99, 2) is None)

    n = c.update_members(1, [
        {"user_id": 7, "card": "李四", "nickname": "四儿"},
        {"user_id": 8, "nickname": "王五"},
        {"user_id": 9},                      # 没名字，跳过
        " garbage ",                          # 非 dict，跳过
    ])
    check("整群列表灌表", n == 2, "%d 条" % n)
    check("有群名片就用群名片", c.member_name(1, 7) == "李四")
    check("没群名片用昵称", c.member_name(1, 8) == "王五")
    check("没名字的不进表", c.member_name(1, 9) is None)

    # ---- parse_event 带上成员表 ----
    payload = {"post_type": "message", "message_type": "group",
               "group_id": 1, "user_id": 2, "sender": {"nickname": "张三"},
               "message": [{"type": "at", "data": {"qq": "7"}},
                           {"type": "text", "data": {"text": " 看下"}}],
               "raw_message": "[CQ:at,qq=7] 看下", "time": 1}
    e = parse_event(payload, names={"7": "李四"})
    check("有名字时当场解析", e.text == "@李四 看下", repr(e.text))
    check("全解析出来了就不用留原文", e.text_raw is None, repr(e.text_raw))

    e2 = parse_event(payload)          # 没有成员表
    check("没名字时先出 @…", "@…" in e2.text, repr(e2.text))
    check("没名字时留原文等回填", e2.text_raw is not None, repr(e2.text_raw))
    check("留的原文里是占位符不是号码",
          at_token(0) in (e2.text_raw or ""), repr(e2.text_raw))
    check("at_ids 能列出没解析的人", e2.at_ids() == ["7"], repr(e2.at_ids()))

    # ---- Store 回填：这是"名字到了把屏幕上的 @… 换掉"那条路 ----
    st = Store()
    st.add(e2)                         # 此时 @… ，号码不可见
    st.add(ev("张三", "不带 @ 的消息", sid=1))
    check("存进去时还是 @…",
          "@…" in st.get(("group", 1))["msgs"][0].text)

    def resolver(gid, uid):
        return {"7": "李四"}.get(uid)

    changed = st.refresh_at_names(resolver)
    check("回填改了 1 条", changed == 1, "%d 条" % changed)
    check("回填后是群名片",
          st.get(("group", 1))["msgs"][0].text == "@李四 看下",
          repr(st.get(("group", 1))["msgs"][0].text))
    check("回填后再调一次不再变更（幂等）",
          st.refresh_at_names(resolver) == 0)
    check("没有 @ 的消息不受影响",
          st.get(("group", 1))["msgs"][1].text == "不带 @ 的消息")

    # ★ 端到端：全程走完，屏幕上**不能出现 QQ 号**
    st2 = Store()
    st2.add(parse_event(payload))      # 名字还没到
    lines_before = message_lines(st2.get(("group", 1))["msgs"], TEXT_W)
    flat_before = "".join(t for t, _ in lines_before)
    check("回填前：屏幕上没有 QQ 号",
          "7" not in flat_before.replace(" ", "").replace("…", ""),
          repr(flat_before))
    st2.refresh_at_names(resolver)
    flat_after = "".join(t for t, _ in
                         message_lines(st2.get(("group", 1))["msgs"], TEXT_W))
    check("回填后：显示的是群名片", "李四" in flat_after, repr(flat_after))
    check("回填后：仍然没有 QQ 号",
          "7" not in flat_after.replace(" ", ""), repr(flat_after))
    check("回填后每行仍不超宽",
          all(text_width(t) <= TEXT_W for t, _ in
              message_lines(st2.get(("group", 1))["msgs"], TEXT_W)))

    # ---- 引用里的 @ 也要解析 ----
    p3 = {"post_type": "message", "message_type": "group",
          "group_id": 1, "user_id": 2, "sender": {"nickname": "张三"},
          "message": [{"type": "reply", "data": {"id": "9"}},
                      {"type": "text", "data": {"text": "同意"}}],
          "raw": {"records": [{
              "sender": {"user_id": 5},
              "elements": [{"textElement": {"content": "看下"}}]}]},
          "raw_message": "同意", "time": 1}
    e3 = parse_event(p3)
    check("引用里有发送者 QQ 号", e3.reply.sender_id == 5,
          repr(e3.reply.sender_id))

    # ---- 真实 payload 长得像这样（字段齐全）----
    real = {"post_type": "message", "message_type": "group",
            "group_id": 894859599, "user_id": 10001,
            "sender": {"nickname": "某人", "card": "某人的群名片"},
            "message": [{"type": "at", "data": {"qq": "10002"}},
                        {"type": "text", "data": {"text": " 出来挨打"}}],
            "raw_message": "[CQ:at,qq=10002] 出来挨打",
            "time": 1700000000, "self_id": 999}
    c2 = OneBotClient("127.0.0.1", 1)
    c2.feed_member(894859599, 10002, "受害者")
    e4 = parse_event(real, 999, c2.members)
    check("真实 payload 解析出群名片", e4.text == "@受害者 出来挨打",
          repr(e4.text))
    check("真实 payload 里没有 QQ 号",
          "10002" not in e4.text, repr(e4.text))


def test_parse_reply_payload():
    """从真实 OneBot payload 里解析引用。"""
    print("\n[引用 —— 协议解析]")

    from onebot import find_reply_seg, reply_from_raw_records

    msg = [{"type": "reply", "data": {"id": "749123"}},
           {"type": "text", "data": {"text": "同意"}}]
    payload = {"post_type": "message", "message_type": "group",
               "group_id": 1, "user_id": 2, "sender": {"nickname": "张三"},
               "message": msg, "raw_message": "[CQ:reply,id=749123]同意",
               "time": 1}
    e = parse_event(payload)
    check("解析出 reply_to", e.reply.message_id == "749123",
          repr(e.reply.message_id))
    check("正文里没有 [回复] 占位",
          "[回复]" not in e.text, repr(e.text))
    check("正文就是'同意'", e.text == "同意", repr(e.text))

    # raw.records 里有内容 → 直接用，不用调 get_msg
    payload2 = dict(payload)
    payload2["raw"] = {"records": [{
        "sendNickName": "李四",
        "elements": [{"textElement": {"content": "明天开会"}}],
        "sender": {"user_id": 456}}]}
    e2 = parse_event(payload2)
    check("raw.records 优先取到引用",
          e2.reply.text == "明天开会" and e2.reply.sender == "李四",
          repr((e2.reply.text, e2.reply.sender)))
    check("raw.records 里也挖出了发送者 QQ 号（@ 要用）",
          e2.reply.sender_id == 456, repr(e2.reply.sender_id))

    # 没有 raw.records → 留空，等后台线程补
    check("没有 records 时留给后台补",
          e.reply.text == "" and e.reply.sender == "")

    # 没有引用时 ev.reply 必须是 None（渲染层按这个判断有没有引用行）
    e3 = parse_event({"post_type": "message", "message_type": "group",
                      "group_id": 1, "user_id": 2,
                      "sender": {"nickname": "张三"},
                      "message": [{"type": "text", "data": {"text": "在"}}],
                      "raw_message": "在", "time": 1})
    check("没有引用时 ev.reply 是 None", e3.reply is None, repr(e3.reply))

    # 找引用段
    check("find_reply_seg 能找到",
          find_reply_seg(msg) == {"id": "749123"})
    check("没有引用段返回 None",
          find_reply_seg([{"type": "text", "data": {}}]) is None)
    check("message 不是数组时不炸", find_reply_seg("raw") is None)


def test_keyboard_layout():
    """键盘布局的算术 —— 每行必须**正好铺满 240px**。

    铺不满会在右边留一条缝，手指掉进去什么都不发生；超了会在右边
    被裁掉，最右边的键点不到。24px 是竖屏 10 键的极限（= 4.3mm，
    只有指尖的一半），所以宽度没有富余，必须严格凑满。
    """
    print("\n[键盘布局]")
    from app import KB_PAGES, KB_ROW2_INDENT, KB_TOP, KB_ROW_H
    V = appmod = __import__("app")
    # 四页几何完全一致，检查其中任意一页就行
    KB_LAYOUT = KB_PAGES["cn"]

    for r, row in enumerate(KB_LAYOUT):
        # 第 2 行（asdfghjkl）只有 9 个键，**左右各缩进** 12px 才铺满。
        # 缩进只算一边的话会得出 228px，然后误以为右边留了条缝。
        pad = KB_ROW2_INDENT * 2 if r == 1 else 0
        total = sum(kw for _lbl, kw, _act in row) + pad
        check("第 %d 行正好铺满 %dpx" % (r + 1, V.W), total == V.W,
              "实际 %dpx" % total)

    # 每行最后一个键的右边界不能超出屏幕
    for r, row in enumerate(KB_LAYOUT):
        x = KB_ROW2_INDENT if r == 1 else 0
        right = x + sum(kw for _l, kw, _a in row)
        check("第 %d 行右边界不超过 %d" % (r + 1, V.W), right <= V.W,
              "%d" % right)

    # 第 2 行的缩进必须是**对称**的，否则整排键会偏
    row2 = KB_LAYOUT[1]
    right_gap = V.W - (KB_ROW2_INDENT + sum(kw for _l, kw, _a in row2))
    check("第 2 行两边缩进对称", right_gap == KB_ROW2_INDENT,
          "左 %d 右 %d" % (KB_ROW2_INDENT, right_gap))

    # 键盘底部不能超出屏幕
    bottom = KB_TOP + len(KB_LAYOUT) * KB_ROW_H
    check("键盘底部不超出屏幕", bottom <= V.H, "%d > %d" % (bottom, V.H))

    # 键盘不能压到候选栏
    from app import CAND_TOP
    check("键盘不压候选栏", KB_TOP >= CAND_TOP + 24,
          "KB_TOP=%d CAND_TOP+24=%d" % (KB_TOP, CAND_TOP + 24))

    # 26 个字母一个都不能少（qwertyuiop + asdfghjkl + zxcvbnm）
    letters = set()
    for row in KB_LAYOUT:
        for label, _kw, act in row:
            if act[0] == "ch" and act[1].isalpha():
                letters.add(act[1])
    check("26 个字母齐全", letters == set("abcdefghijklmnopqrstuvwxyz"),
          "缺: %s" % (set("abcdefghijklmnopqrstuvwxyz") - letters))

    # 功能键必须在
    kinds = set()
    for row in KB_LAYOUT:
        for _l, _kw, act in row:
            kinds.add(act[0])
    for need in ("shift", "back", "space", "send", "mode", "voice"):
        check("有 %s 键" % need, need in kinds, repr(sorted(kinds)))

    # ⚠️ 所有键的字都必须是 BMP 内的。🎤 是 U+1F3A4，传进 canvas
    #    会直接抛 TclError 把程序干掉（这个坑 clean() 那里踩过）。
    bad = []
    for row in KB_LAYOUT:
        for label, _kw, _act in row:
            for ch in label:
                if ord(ch) > 0xFFFF:
                    bad.append((label, hex(ord(ch))))
    check("键上的字都在 BMP 内（emoji 会让 Tk 崩）", not bad, repr(bad))


def test_cursor_placement():
    """光标按**像素**定位，不按字符数。

    中文 17px、ASCII 11px，同样 5 个字符能差 40px。按字符数算光标
    会落在字中间或者下一个字上（这个坑在截断那里已经踩过一次）。
    """
    print("\n[光标定位]")
    from app import wrap_with_cursor, TEXT_W

    # 空文本：光标在起点
    lines, r, x = wrap_with_cursor("", 0, TEXT_W)
    check("空文本有一行", lines == [""], repr(lines))
    check("空文本光标在 0", r == 0 and x == 0, repr((r, x)))

    # 全 ASCII："abc|" —— 每个字符宽度一样，光标应在 3 个字符宽处
    lines, r, x = wrap_with_cursor("abc", 3, TEXT_W)
    one = text_width("a")
    check("ASCII 光标位置对", x == one * 3, "%d vs %d" % (x, one * 3))
    check("ASCII 都在一行", r == 0, repr(r))

    # 全中文："abc" 换成中文，"中中中|"
    lines, r, x = wrap_with_cursor("中中中", 3, TEXT_W)
    one = text_width("中")
    check("中文光标位置对", x == one * 3, "%d vs %d" % (x, one * 3))

    # ★ 关键：中英混排时光标不能按字符数算。
    #   "中a" 光标在 2：位置应该是 17+11 而不是 2*某个平均值
    mixed = "中a"
    lines, r, x = wrap_with_cursor(mixed, 2, TEXT_W)
    want = text_width("中") + text_width("a")
    check("混排光标按像素", x == want, "%d vs %d" % (x, want))

    # 光标在中间
    lines, r, x = wrap_with_cursor("中a", 1, TEXT_W)
    check("光标在中间", x == text_width("中"), repr(x))

    # 折行：超长要换行，光标跟着到第二行
    long_ascii = "a" * 40
    lines, r, x = wrap_with_cursor(long_ascii, 40, TEXT_W)
    check("超长折成多行", len(lines) > 1, "%d 行" % len(lines))
    check("光标落在最后一行", r == len(lines) - 1, "%d/%d" % (r, len(lines)))

    # 每行都不超宽
    over = [l for l in lines if text_width(l) > TEXT_W]
    check("折行后每行不超宽", not over, repr(over[:2]))


class FakeItem(object):
    """仿 unihiker 的 canvas item，**特意把位置存在 .arg 里**。

    ⚠️ 这是这条测试唯一的重点，第一版就是栽在这儿：

    我一开始把假 item 写成 `self.kw = dict(**kw)`，位置也进 kw。
    于是 `_show_input_ui` 里那句 `it.kw.get("x", 0)` 读得**到**坐标，
    测试全绿 —— 但真机上 unihiker 把位置存在 `item.arg`，
    `item.kw` 装的是 Tk 那层的东西（font/fill/…），根本没有 x。
    读出来全是 0，回位时整套控件挪到了 (0,0)，真机上是**一屏左上角
    糊在一起的按钮**，而我的测试还在报"全部通过"。

    所以这里必须照真库的样子存：**位置进 .arg，kw 里不留 x/y**。
    假件比真件"好说话"，测试就是在骗自己。
    """

    def __init__(self, kind, **kw):
        self.kind = kind
        self.arg = {"x": kw.pop("x", 0), "y": kw.pop("y", 0)}
        self.kw = dict(kw)          # ← 故意不含 x/y
        self.kw_current = {}
        self.state = "hidden"

    def config(self, **kw):
        if "x" in kw:
            self.arg["x"] = kw.pop("x")
        if "y" in kw:
            self.arg["y"] = kw.pop("y")
        self.kw.update(kw)
        if "state" in kw:
            self.state = kw["state"]

    @property
    def pos(self):
        return (self.arg["x"], self.arg["y"])


class FakeGUI(object):
    def __init__(self, w=240, h=320):
        self.w, self.h, self.font_name = w, h, "fake"

    def draw_text(self, **kw):
        return FakeItem("text", **kw)

    def fill_rect(self, **kw):
        return FakeItem("rect", **kw)


def test_input_ui_show_hide():
    """输入界面的显示/隐藏必须**能来回切**，而且位置不能跑到左上角。

    ⚠️ 两个 unihiker 特有的坑，都在真机上踩过：

    1. `config()` 签名里**没有** state（靠 **kw 转给 canvas.itemconfigure），
       而且矩形是它自己按 fill 决定 state 的（GUI.py:
       `if fill != '': state='normal' else: state='hidden'`）。
       所以只靠 state 隐藏可能被覆盖掉 —— 得再挪到屏幕外。

    2. **位置不能反过来问 item 要**。unihiker 存在 item.arg，
       item.kw 里没有 x/y。第一版读 kw 读出 0，回位时全糊到左上角。
       FakeItem 特意模仿这个结构，见它的 docstring。
    """
    print("\n[输入界面 —— 显示/隐藏往返]")

    import app as appmod

    t = appmod.QQTerm.__new__(appmod.QQTerm)
    t.g = FakeGUI()
    t.title = FakeItem("text", text="")
    t.hint_msg = ""
    appmod.QQTerm._build_input_ui(t, t.g)

    check("建出了输入界面的 item", len(t.input_items) > 30,
          "%d 个" % len(t.input_items))
    check("有文本行", len(t.in_rows) == appmod.TEXT_LINES)
    check("有候选位", len(t.in_cands) == appmod.CAND_COUNT,
          "%d 个（应为 %d）" % (len(t.in_cands), appmod.CAND_COUNT))
    check("候选有两个翻页箭头",
          t.cand_prev is not None and t.cand_next is not None)
    check("键数够多", len(t.keys) >= 30, "%d 个键" % len(t.keys))

    key = t.keys[0][0]
    home = key.pos
    check("★ 第一个键不在左上角（建的时候就该在 0,164）",
          home != (0, 0), repr(home))

    appmod.QQTerm._show_input_ui(t, True)
    check("显示后键在原位", key.pos == home, repr(key.pos))
    check("显示后 state 是 normal", key.state == "normal", key.state)

    appmod.QQTerm._show_input_ui(t, False)
    off = key.pos
    check("隐藏后被挪出屏幕",
          off[0] > appmod.W or off[1] > appmod.H, repr(off))

    appmod.QQTerm._show_input_ui(t, True)
    check("★ 再显示能**原样**回位（跑到左上角就是这个 bug）",
          key.pos == home, "%r != %r" % (key.pos, home))

    # 连续隐藏两次不能把"原位"弄丢
    appmod.QQTerm._show_input_ui(t, False)
    appmod.QQTerm._show_input_ui(t, False)
    appmod.QQTerm._show_input_ui(t, True)
    check("连隐两次后仍能回位", key.pos == home, repr(key.pos))

    # ★ 每一个 item 都要回位，不能只有第一个键对。
    #   这就是真机上"按钮全糊左上角"的复现点。
    appmod.QQTerm._show_input_ui(t, False)
    appmod.QQTerm._show_input_ui(t, True)
    stray = [(it, it.pos) for it, hx, hy in t.input_items if it.pos == (0, 0)]
    check("★ 没有 item 掉到左上角", not stray,
          "%d 个在 (0,0): %r" % (len(stray), [s[1] for s in stray[:3]]))
    outside = [(it, it.pos) for it, hx, hy in t.input_items
               if it.pos[0] > appmod.W or it.pos[1] > appmod.H]
    check("没有 item 留在屏幕外", not outside, repr(outside[:3]))

    # 回位必须回到**各自的**位置，不是大家一起回同一个点
    positions = set(it.pos for it, hx, hy in t.input_items)
    check("回位后各 item 位置互不相同（没有被挤到一起）",
          len(positions) > 20, "只有 %d 个不同位置" % len(positions))

    # ★ 反向验证：如果照第一版那样从 kw 读坐标，会发生什么
    kw_x = key.kw.get("x", 0)
    check("（反向）从 item.kw 读坐标会得到 0 —— 这就是当初的 bug",
          kw_x == 0, "kw 里居然有 x=%r，这个假件没模仿对" % kw_x)


def test_exit_button_dispatch():
    """右上角 ✕ 在输入界面里必须是**收起键盘**，不能退出程序。

    这是真机上用户报的：键盘上打字时想关键盘，一点 ✕ 整个程序就没了，
    打了一半的内容也没了。

    ✕ 是全局按钮 —— 每个界面都画在同一个位置、挂同一个回调，
    所以回调里必须按 self.view 分派。
    """
    print("\n[✕ 键 —— 按界面分派]")

    import app as appmod

    class T(object):
        """只带 _request_quit 需要的那些属性。"""
        _request_quit = appmod.QQTerm._request_quit
        close_input = lambda self: self.calls.append("close_input")   # noqa
        show_chat = lambda self: self.calls.append("show_chat")       # noqa

        def __init__(self, view, cur=("group", 1)):
            self.view = view
            self.cur = cur
            self.quit_requested = False
            self.calls = []
            self.buffer = type("B", (), {
                "commit_compose": lambda s: self.calls.append("commit")})()

    # 输入界面：收起键盘，**不退出**
    t = T(appmod.VIEW_INPUT)
    t._request_quit()
    check("★ 输入界面点 ✕ 不退出程序", t.quit_requested is False,
          "quit_requested=%r" % t.quit_requested)
    check("输入界面点 ✕ 收起键盘", "close_input" in t.calls, repr(t.calls))
    check("输入界面点 ✕ 回聊天页", "show_chat" in t.calls, repr(t.calls))
    check("输入界面点 ✕ 先把拼音上屏（别丢字）",
          "commit" in t.calls, repr(t.calls))

    # 其它界面：照旧退出
    for view in (appmod.VIEW_SESSIONS, appmod.VIEW_CHAT,
                 appmod.VIEW_MENU, appmod.VIEW_REPLY):
        t = T(view)
        t._request_quit()
        check("%s 点 ✕ 退出程序" % view, t.quit_requested is True,
              repr(t.quit_requested))

    # 反向：旧的实现（直接 quit）在输入界面会干什么
    class Old(T):
        _request_quit = lambda self: setattr(self, "quit_requested", True)

    t = Old(appmod.VIEW_INPUT)
    t._request_quit()
    check("（反向）旧写法在键盘上会把程序退掉", t.quit_requested is True)


def test_footer_hidden_in_input():
    """进键盘时，底部提示行必须**藏起来**。

    底部那两行提示（y=268/292）和分隔线（y=264）正压在键盘上
    （键盘占 y=164..320）。不藏的话"点会话进入""A上 B下"会从
    键缝里透出来。用户原话："只留在键盘那里后面不显示"。
    """
    print("\n[键盘界面 —— 底部提示要藏]")

    import app as appmod

    t = appmod.QQTerm.__new__(appmod.QQTerm)
    t.g = FakeGUI()
    t.title = FakeItem("text", text="")
    t.hint_msg = ""
    appmod.QQTerm._build_input_ui(t, t.g)
    # 底部那三样是 _build_input_ui 之外的 __init__ 里建的，
    # 这里手工摆一个，验显隐逻辑本身
    t.footer_items = []
    for i, y in enumerate((264, 268, 292)):
        it = FakeItem("text", x=appmod.PAD, y=y)
        t.footer_items.append((it, appmod.PAD, y))
    t.hint = t.footer_items[1][0]
    t.hint2 = t.footer_items[2][0]

    # 进输入界面 → 底部藏起来
    appmod.QQTerm._show_input_ui(t, True)
    hidden = [it.pos for it, _x, _y in t.footer_items
              if it.pos[0] > appmod.W or it.pos[1] > appmod.H]
    check("★ 进键盘后底部提示被藏起来",
          len(hidden) == len(t.footer_items),
          "%d/%d 藏了" % (len(hidden), len(t.footer_items)))

    # 键盘自己要显示出来
    key = t.keys[0][0]
    check("键盘显示出来了",
          key.pos[0] <= appmod.W and key.pos[1] <= appmod.H, repr(key.pos))

    # 退出输入界面 → 底部回来，键盘藏起来
    appmod.QQTerm._show_input_ui(t, False)
    back = [it.pos for it, _x, _y in t.footer_items
            if it.pos[0] <= appmod.W and it.pos[1] <= appmod.H]
    check("★ 退出键盘后底部提示回来",
          len(back) == len(t.footer_items),
          "%d/%d 回来了" % (len(back), len(t.footer_items)))
    check("退出键盘后键盘藏起来",
          key.pos[0] > appmod.W or key.pos[1] > appmod.H, repr(key.pos))

    # 来回切几次不能乱
    for _ in range(3):
        appmod.QQTerm._show_input_ui(t, True)
        appmod.QQTerm._show_input_ui(t, False)
    pos = [it.pos for it, _x, _y in t.footer_items]
    check("来回切完底部回原位", all(p[1] in (264, 268, 292) for p in pos),
          repr(pos))


def test_seed_from_roster():
    """★ 不用等群来消息，就该能看到群和好友。

    用户原话："目前要等一个群来消息之后，我才能在里面发消息，我们得
    想个办法改，就比如改成默认显示群和好友的"。

    以前 Store 只在收到消息时建会话 —— 等于"没人说话就什么都看不到"，
    想主动发消息得等对方先开口。现在连上后拉 get_group_list /
    get_friend_list，直接建出空会话。
    """
    print("\n[从名单建会话 —— 不用等消息]")

    s = Store()
    check("一开始什么会话都没有", not s.has_any())

    # 灌群名单
    groups = {"894859599": "异世界卡车群", "112233445": "技术交流群"}
    n = s.seed(groups=groups)
    check("★ 群名单建出了会话", n == 2, "%d 个" % n)
    check("★ 没消息也能看到群",
          len(s.ordered()) == 2, repr([k for k, _ in s.ordered()]))
    check("会话名用的是群名，不是号码",
          all(v["name"] in groups.values() for _k, v in s.ordered()),
          repr([v["name"] for _k, v in s.ordered()]))
    check("空会话没有消息", all(v["msgs"] == [] for _k, v in s.ordered()))

    # 灌好友名单
    n2 = s.seed(friends={"10001": "张三", "10002": "李四"})
    check("★ 好友名单也建出了会话", n2 == 2, "%d 个" % n2)
    check("现在有 4 个会话", len(s.ordered()) == 4,
          repr([k for k, _ in s.ordered()]))
    kinds = set(k[0] for k, _ in s.ordered())
    check("群和好友都在", kinds == {"group", "private"}, repr(kinds))

    # 幂等：再灌一遍不能重复建
    n3 = s.seed(groups=groups, friends={"10001": "张三"})
    check("★ 重复 seed 不会重复建会话", n3 == 0 and len(s.ordered()) == 4,
          "新建 %d 个，共 %d 个" % (n3, len(s.ordered())))

    # ★ 来消息的会话要排到空会话前面
    s.add(ev("张三", "你好", kind="group", sid=112233445))
    top = s.ordered()[0][0]
    check("★ 有消息的顶到最前", top == ("group", 112233445), repr(top))
    check("空会话垫底",
          s.ordered()[-1][1]["msgs"] == [], repr(s.ordered()[-1][0]))

    # ★ 空会话的顺序要稳定 —— 每次刷新都换位置的话手指会点错
    s2 = Store()
    s2.seed(groups={"1": "A", "2": "B", "3": "C", "4": "D"})
    order1 = [k for k, _ in s2.ordered()]
    for _ in range(5):
        s2.seed(groups={"1": "A"})       # 反复 seed
    order2 = [k for k, _ in s2.ordered()]
    check("★ 空会话顺序稳定（不会每次刷新都跳）", order1 == order2,
          repr((order1, order2)))

    # ensure 幂等，且不冲掉已有消息
    s3 = Store()
    s3.add(ev("张三", "第一条", sid=1))
    before = len(s3.get(("group", 1))["msgs"])
    s3.ensure("group", 1, "改名了")
    check("★ ensure 不冲掉已有消息",
          len(s3.get(("group", 1))["msgs"]) == before,
          "%d -> %d" % (before, len(s3.get(("group", 1))["msgs"])))

    # ensure 补名字（会话还没消息时可以改名）
    s4 = Store()
    s4.ensure("group", 7, "老名字")
    s4.ensure("group", 7, "新名字")
    check("空会话能改名", s4.get(("group", 7))["name"] == "新名字",
          repr(s4.get(("group", 7))["name"]))

    # ★ 给没说过话的会话发消息：不能被丢掉
    s5 = Store()
    s5.seed(groups={"555": "没说过话的群"})
    s5.add_self(("group", 555), "我先说话")
    check("★ 能给空会话发消息并留在列表里",
          len(s5.get(("group", 555))["msgs"]) == 1,
          repr(s5.get(("group", 555))["msgs"]))

    # ★ 键的类型必须统一 —— 否则同一个群会建出**两个**会话
    #
    #   消息来的 sid 是 int（JSON 数字）
    #   名单缓存的键是 str（我们存的时候写了 str(gid)）
    #   两个 key 不相等 → 列表里同一个群出现两次，一个带消息一个空。
    #   （本地集成检查抓到的，真机上也一样。）
    s9 = Store(group_names={"894859599": "异卡群"})
    s9.add(ev("张三", "消息来的", kind="group", sid=894859599))    # int
    s9.seed(groups={"894859599": "异卡群"})                        # str
    check("★ 消息和名单建出的是**同一个**会话（键类型要统一）",
          len(s9.sessions) == 1,
          "建出了 %d 个: %r" % (len(s9.sessions), list(s9.sessions)))
    check("★ 没有重复的群出现在列表里", len(s9.ordered()) == 1,
          repr([(k, v["name"]) for k, v in s9.ordered()]))

    # 反过来也要成立：先 seed（str）再来消息（int）
    s10 = Store()
    s10.seed(groups={"123456": "先建的"})
    s10.add(ev("李四", "后来的", kind="group", sid=123456))
    check("★ 先名单后消息也只有一个会话", len(s10.sessions) == 1,
          repr(list(s10.sessions)))
    check("消息进来了", len(s10.ordered()[0][1]["msgs"]) == 1,
          repr(s10.ordered()[0][1]["msgs"]))

    # norm_sid 本身
    from app import Store as _S
    check("int 原样", _S.norm_sid(123) == 123)
    check("数字串转 int", _S.norm_sid("123") == 123,
          repr(_S.norm_sid("123")))
    check("非数字串原样保留", _S.norm_sid("abc") == "abc",
          repr(_S.norm_sid("abc")))

    # 反向：老行为 —— 只在收消息时建会话
    s6 = Store()
    check("（反向）只等消息的话，一开始列表是空的", not s6.has_any())
    check("（反向）空列表上没法发消息（这就是要改的问题）",
          s6.get(("group", 894859599)) is None)

    # ★ 私聊：自己先开口时，会话名必须是**对方**的名字，不能是"我"
    #   也不能是 QQ 号。
    #   场景：给一个从没聊过的好友发第一条消息。
    friends = {"50001": "从没聊过的好友"}
    s7 = Store(friend_names=friends)
    s7.add_self(("private", 50001), "我先说话")
    nm = s7.get(("private", 50001))["name"]
    check("★ 给没聊过的好友发消息，会话名是对方名字",
          nm == "从没聊过的好友", repr(nm))
    check("★ 不是「我」", nm != "我", repr(nm))
    check("★ 也不是 QQ 号", nm != "50001", repr(nm))

    # 好友表里没有的号，退回号码（拿不到名字总得有个标识）
    s8 = Store(friend_names={})
    s8.add_self(("private", 99999), "发给陌生人")
    check("查不到名字时退回号码",
          s8.get(("private", 99999))["name"] == "99999",
          repr(s8.get(("private", 99999))["name"]))


def test_friend_list_parsing():
    """get_friend_list 的解析 —— 备注优先于昵称。"""
    print("\n[好友列表解析]")

    from onebot import OneBotClient

    c = OneBotClient("127.0.0.1", 1)
    check("一开始没有好友", c.lookup_friend(10001) is None)

    c._on_friend_list({"data": [
        {"user_id": 10001, "nickname": "张三", "remark": "老张"},
        {"user_id": 10002, "nickname": "李四"},
        {"user_id": 10003},              # 没名字，跳过
        "garbage",                        # 非 dict，跳过
    ]})
    check("★ 有备注就用备注", c.lookup_friend(10001) == "老张",
          repr(c.lookup_friend(10001)))
    check("没备注用昵称", c.lookup_friend(10002) == "李四",
          repr(c.lookup_friend(10002)))
    check("没名字的不进表", c.lookup_friend(10003) is None)
    check("未知好友返回 None（不是占位）",
          c.lookup_friend(99999) is None)
    check("friend_name 有默认值", c.friend_name(99999) == "99999",
          repr(c.friend_name(99999)))

    # 空响应不炸
    c._on_friend_list(None)
    c._on_friend_list({})
    c._on_friend_list({"data": "坏的"})
    check("坏数据不炸", c.lookup_friend(10001) == "老张")

    # 接口一致性：演示客户端也得有这些方法
    from democlient import DemoClient
    d = DemoClient()
    for m in ("load_friends", "lookup_friend", "friend_name",
              "_on_friend_list"):
        check("演示客户端有 %s" % m, hasattr(d, m))


def test_send_two_step():
    """★ 发送键必须**两步确认** —— 用户报"发送键很容易误触"。

    发送键在右下角 48px，那是右手拇指自然搁着的位置，而它一点就发。
    打字的功夫拇指搭上去，消息就飞出去了。QQ 消息**发出去收不回来**，
    误触的代价远大于多点一下。

    所以：第一次点变成"确认"（红色），第二次才真发。
    敲任何别的键都取消这个状态。
    """
    print("\n[发送键 —— 两步确认]")

    import app as appmod

    sent = []

    class T(object):
        _send_from_input = appmod.QQTerm._send_from_input

        def __init__(self, text="你好"):
            self.buffer = TextBuffer()
            if text:
                self.buffer.insert(text)
            self.send_armed = False
            self.hint_msg = ""
            self.store = Store()
            self.store.add(ev("张三", "在吗", sid=1))
            self.cur = ("group", 1)
            self.msg = ""
            self.scroll = 0
            self.client = type("C", (), {
                "send": lambda _s, k, sid, t, cb=None: (
                    sent.append(t), True)[1]})()

        def _draw_input(self):
            pass

    # 1) 第一次点：**不能发出去**
    t = T("你好")
    t._send_from_input()
    check("★ 第一次点不发送", not sent, "却发了: %r" % sent)
    check("第一次点进入待确认", t.send_armed is True)
    check("第一次点有提示", "再点" in t.hint_msg, repr(t.hint_msg))

    # 2) 第二次点：真发
    t._send_from_input()
    check("★ 第二次点才发送", sent == ["你好"], repr(sent))
    check("发完清掉待确认", t.send_armed is False)
    check("发完清空输入框", t.buffer.text == "", repr(t.buffer.text))

    # 3) ★ 中途改字要取消待确认 ——
    #    否则"点一下(变红) → 改错字 → 再点"会把**改完的**直接发出去，
    #    用户以为还是第一次确认
    sent[:] = []
    t2 = T("错字")
    t2._send_from_input()               # 第一次点，变红
    check("进入待确认", t2.send_armed is True)
    t2.buffer.clear()
    t2.buffer.insert("改好的")           # 用户改了内容
    # 模拟 _do_key 里那段"敲任何键都取消"
    if t2.send_armed:
        t2.send_armed = False
    t2._send_from_input()               # 再点 —— 应该只是再次进入确认
    check("★ 改过字之后再点只是重新进入确认，不直接发",
          not sent, "却发了: %r" % sent)
    check("此时处于待确认", t2.send_armed is True)
    t2._send_from_input()               # 第三次才真发
    check("第三次点才发出改好的内容", sent == ["改好的"], repr(sent))

    # 4) 空内容不发
    sent[:] = []
    t3 = T("")
    t3._send_from_input()
    check("空内容不发送", not sent, repr(sent))
    check("空内容不进入待确认", t3.send_armed is False)

    # 5) 拼音没选字，点了发送要先上屏再确认（别丢字）
    t4 = T("")
    t4.buffer.feed_key("n")
    t4.buffer.feed_key("i")
    t4._send_from_input()               # 第一次点
    check("拼音被落顶上屏（不丢字）", t4.buffer.text == "ni",
          repr(t4.buffer.text))
    check("确认等待中", t4.send_armed is True)
    t4._send_from_input()
    check("发出来的是落屏后的内容", sent[-1] == "ni", repr(sent))

    # 6) 反向：如果只有一步，误触就直接发出去了
    class OneStep(T):
        _send_from_input = None

    def one_step(self):
        b = self.buffer
        if b.composing:
            b.commit_compose()
        self.client.send("group", 1, b.text.strip())

    t5 = T("误触的内容")
    one_step(t5)
    check("（反向）一步发送时误触直接就发出去了", sent[-1] == "误触的内容",
          repr(sent[-1]))


def test_no_blank_flicker():
    """刷新时**不能有空屏的那一瞬间**。

    用户报的："文字刷新太快了，它又是从上往下刷的，导致到后面文字
    闪一下，又进入刷新从上往下刷了，就闪一下看不到。"

    根因是刷新的顺序：先把 9 行正文**全部置空**，再一行行填新的。
    中间那一帧屏幕是空的 —— 群消息一来一回很快，看着就是闪。

    修法：**先写后清**。先把新内容覆盖上去，再把多出来的行清掉。
    这样任何一帧屏幕上都有完整内容。

    这条测试就盯着"有没有那么一帧"。做法：给每一行记一个"被写空的
    时刻"，如果某一行先被写成空、之后才被填上非空，就是闪了。
    """
    print("\n[刷新不能闪 —— 先写后清]")

    import app as appmod

    class Row(object):
        """记录每一行的完整改写历史。"""

        def __init__(self):
            self.history = []

        def config(self, **kw):
            if "text" in kw:
                self.history.append(kw["text"])

    class T(object):
        _draw_chat_body = appmod.QQTerm._draw_chat_body

        def __init__(self, n=9):
            self.rows = [Row() for _ in range(n)]
            self.store = Store()
            self.cur = ("group", 1)
            self.scroll = 0
            self.title = Row()
            self.hint = Row()
            self.hint2 = Row()
            self.msg = ""
            self.client = type("C", (), {"state": "online"})()

        def _set_status(self):
            pass

        def _state_text(self):
            return "在线"

        def show_sessions(self):
            pass

    def blanks_then_fills(rows, baseline):
        """检查有没有行"先被清空、之后又被填回来"。

        ⚠️ 判据要精细。不是所有"空"都算闪：

            row 1: [''] → ['李四: 在吗']      正常！这行本来就没内容，
                                              现在有了一条新消息而已
            row 1: ['张三: 你好'] → [''] → ['李四: 在吗']
                                              ← **这才是闪**：
                                              本来有字，先被抹了再写

        所以基准是**上一次刷新结束时的样子**（baseline），
        只有"上次非空、这次先变空"才算闪。
        """
        flick = []
        for i, r in enumerate(rows):
            was_filled = bool(baseline[i]) if i < len(baseline) else False
            if not was_filled:
                continue
            saw_blank_after = False
            for t in r.history:
                if t == "":
                    saw_blank_after = True
                elif saw_blank_after:
                    flick.append(i)
                    break
        return flick

    def snapshot(rows):
        return [r.history[-1] if r.history else "" for r in rows]

    # 1) 第一次画（从空开始）不算闪 —— 那时本来就什么都没有
    t = T()
    t.store.add(ev("张三", "你好"))
    t._draw_chat_body()
    check("第一次画能出内容",
          any(r.history and r.history[-1] for r in t.rows),
          repr([r.history for r in t.rows[:3]]))

    # 2) ★ 关键：已经有内容了，再来一条新消息
    base = snapshot(t.rows)
    for r in t.rows:
        r.history = []
    t.store.add(ev("李四", "在吗"))
    t._draw_chat_body()
    flick = blanks_then_fills(t.rows, base)
    check("★ 追加消息时没有行被闪空", not flick,
          "这些行先被清空又填上: %r" % flick)
    check("新消息确实显示出来了",
          "李四" in "".join(snapshot(t.rows)), repr(snapshot(t.rows)[:3]))

    # 3) 反复追加，每一次都不能闪
    bad_rounds = []
    for i in range(6):
        base = snapshot(t.rows)
        for r in t.rows:
            r.history = []
        t.store.add(ev("张三", "消息 %d" % i))
        t._draw_chat_body()
        f = blanks_then_fills(t.rows, base)
        if f:
            bad_rounds.append((i, f))
    check("★ 连续 6 次刷新都不闪", not bad_rounds, repr(bad_rounds[:2]))

    # 3b) ★ 满屏滚动时最容易闪 —— 内容整体上移，每一行都换内容
    t5 = T()
    for i in range(12):
        t5.store.add(ev("张三", "第 %d 条消息" % i))
    t5._draw_chat_body()
    bad_scroll = []
    for i in range(8):
        base = snapshot(t5.rows)
        for r in t5.rows:
            r.history = []
        t5.store.add(ev("李四", "新消息 %d" % i))
        t5._draw_chat_body()
        f = blanks_then_fills(t5.rows, base)
        if f:
            bad_scroll.append((i, f))
    check("★ 满屏滚动追加也不闪", not bad_scroll, repr(bad_scroll[:2]))

    # 4) 内容变少时（换会话、翻页）多余的行**必须**清掉，
    #    否则旧内容会留在下面冒充新内容
    t2 = T()
    for i in range(12):
        t2.store.add(ev("张三", "第 %d 条" % i))
    t2._draw_chat_body()
    filled = sum(1 for r in t2.rows if r.history and r.history[-1])
    check("消息多时占满屏", filled == len(t2.rows), "%d/9" % filled)

    t3 = T()
    t3.store.add(ev("张三", "只有一条"))
    t3._draw_chat_body()
    leftover = [(i, r.history[-1]) for i, r in enumerate(t3.rows)
                if r.history and r.history[-1]]
    check("★ 消息少时多出来的行被清掉（不留旧内容）",
          len(leftover) <= 2, repr(leftover[:4]))

    # 5) 反向：旧的实现（先清后写）确实会闪
    class Old(T):
        def _draw_chat_body_old(self):
            for r in self.rows:
                r.config(text="")          # ← 先全清
            self._draw_chat_body()          # ← 再写

    t4 = Old()
    for i in range(12):
        t4.store.add(ev("张三", "第 %d 条" % i))
    t4._draw_chat_body()
    base = snapshot(t4.rows)
    for r in t4.rows:
        r.history = []
    t4.store.add(ev("李四", "新的一条"))
    # 手动走一遍**旧**顺序：先全清，再画
    for r in t4.rows:
        r.config(text="")
    t4._draw_chat_body()
    f = blanks_then_fills(t4.rows, base)
    check("（反向）先清后写确实会闪 —— 这就是当初的 bug", bool(f),
          repr(f))


def test_demo_client():
    """演示客户端必须和真客户端**接口一致**。

    这是它能替代 OneBotClient 的**唯一**理由。少了哪个方法，
    app.py 会在某个不起眼的路径上 AttributeError —— 而且往往不是
    一启动就炸，是点某个菜单才炸，很难查。

    所以这里拿两个类对着比：真客户端有的公开方法，演示客户端也得有。
    """
    print("\n[演示模式 —— 接口一致性]")

    from onebot import OneBotClient
    from democlient import (DemoClient, DEMO_MESSAGES, DEMO_GROUPS,
                            DEMO_MEMBERS)

    real = set(m for m in dir(OneBotClient) if not m.startswith("_"))
    demo = set(m for m in dir(DemoClient) if not m.startswith("_"))

    # app.py 真正会用到的那些，一个都不能少
    need = ["start", "stop", "poll", "send", "lookup_group", "group_name",
            "member_name", "member_lookup", "missing_names", "feed_member",
            "update_members", "fetch_reply", "fetch_member_name",
            # 好友相关 —— 少了这些，"不用等消息就能看到好友"那条路
            # 会在演示模式下 AttributeError
            "load_friends", "lookup_friend", "friend_name"]
    missing = [m for m in need if m not in demo]
    check("演示客户端有全部必需方法", not missing, "缺: %s" % missing)

    # 反过来：真客户端有而演示没有的，列出来看看是不是漏了
    only_real = sorted(real - demo)
    print("      真客户端多出来的（演示模式用不到就行）: %s" % only_real)

    # ---- 属性 ----
    d = DemoClient()
    for attr in ("state", "last_error", "groups", "online", "self_id"):
        check("有属性 %s" % attr, hasattr(d, attr))

    check("一开始就是 online（测的是界面不是连接）",
          d.state == "online" and d.online is True, repr(d.state))

    # ---- 群名：查不到必须返回 None，不能返回占位 ----
    #      Store.refresh_names 靠这个判断要不要覆盖名字
    gid = list(DEMO_GROUPS)[0]
    check("查得到群名", d.lookup_group(gid) == DEMO_GROUPS[gid],
          repr(d.lookup_group(gid)))
    check("★ 未知群号返回 None（不能返回'群 123'占位）",
          d.lookup_group("000000") is None, repr(d.lookup_group("000000")))
    check("group_name 有默认值", "群 000000" in d.group_name("000000"),
          repr(d.group_name("000000")))

    # ---- 群名片：查不到也要返回 None（界面显示 @…，不露号码）----
    check("查得到群名片", d.member_name("894859599", "3890902844") == "RC已回归",
          repr(d.member_name("894859599", "3890902844")))
    check("未知成员返回 None",
          d.member_name("894859599", "00000") is None)

    # ---- poll：一开始空，喂了才有 ----
    d2 = DemoClient()
    check("初始队列为空", d2.poll() is None)

    # ---- send：不真的发，但要"成功" ----
    d2 = DemoClient()
    check("send 返回 True", d2.send("group", "123", "测试") is True)
    check("send 记下了内容", d2.sent == [("group", "123", "测试")],
          repr(d2.sent))

    # ---- 假数据的形状：值可以编，**形状**必须是真的 ----
    check("假消息不空", len(DEMO_MESSAGES) >= 15,
          "%d 条" % len(DEMO_MESSAGES))
    bad = []
    for i, item in enumerate(DEMO_MESSAGES):
        if len(item) != 4:
            bad.append((i, "不是 4 元组"))
            continue
        # 名字要和 democlient 里的元组顺序一致：(群号, 发送者QQ号, …)
        gid, sender_uid, segs, extra = item
        if not isinstance(segs, list):
            bad.append((i, "段不是 list"))
        if not isinstance(extra, dict):
            bad.append((i, "extra 不是 dict"))
        for s in segs:
            if not isinstance(s, dict) or "type" not in s:
                bad.append((i, "段缺 type"))
            elif not isinstance(s.get("data"), dict):
                bad.append((i, "段缺 data"))
    check("假消息的形状都是 OneBot 段的样式", not bad, repr(bad[:3]))

    # 覆盖到的场景 —— 这些是排版和解析最容易出问题的地方
    kinds = set()
    at_tests = []
    for gid, sender_uid, segs, extra in DEMO_MESSAGES:
        for s in segs:
            kinds.add(s.get("type"))
            if s.get("type") == "at":
                at_tests.append(s.get("data", {}).get("qq"))
    for need_kind in ("text", "at", "image", "face", "record", "reply"):
        check("覆盖了 %s 段" % need_kind, need_kind in kinds,
              repr(sorted(kinds)))
    check("有 @全体成员", "all" in at_tests, repr(at_tests))
    check("有 @具体的人", any(q and q != "all" for q in at_tests), repr(at_tests))
    check("有 emoji 的消息（会被 clean 成 □）",
          any("🍉" in str(s.get("data", {}).get("text", ""))
              for _g, _u, segs, _e in DEMO_MESSAGES
              for s in segs if s.get("type") == "text"),
          "没找到 emoji 用例")
    check("有超长消息（测折行）",
          any(len(str(s.get("data", {}).get("text", ""))) > 60
              for _g, _u, segs, _e in DEMO_MESSAGES
              for s in segs if s.get("type") == "text"), "没找到超长用例")
    check("有超长群名（测截断）",
          any(len(n) > 16 for n in DEMO_GROUPS.values()), repr(DEMO_GROUPS))

    # ★ 群号和发送者 QQ 号**不能搞反**。
    #   踩过一次：元组是 (群号, 发送者QQ号, …)，解包成了 (kind, sid)，
    #   用的时候也就反着用 —— 结果会话名全成了"群 10001"（那是发送者），
    #   而真群号是 894859599。这种错**不报错**，只是安静地显示错东西。
    #
    #   所以这里用一条不变式钉死：每条假消息的群号，都必须在
    #   DEMO_GROUPS 里有对应条目。反过来的话群号就成了发送者的号，
    #   那条断言立刻红。
    unknown = sorted(set(gid for gid, _u, _s, _e in DEMO_MESSAGES
                         if gid not in DEMO_GROUPS))
    check("★ 每条假消息的群号都在群表里（防止群号/发送者搞反）",
          not unknown, "这些群号不在 DEMO_GROUPS: %s" % unknown)

    # 发送者 QQ 号必须都在成员表里有名字，否则界面上到处是"某人"
    nobody = sorted(set((gid, uid) for gid, uid, _s, _e in DEMO_MESSAGES
                        if (gid, uid) not in DEMO_MEMBERS))
    check("假消息的发送者都在成员表里", not nobody,
          "这些人在成员表里没有: %s" % nobody[:3])

    # 会话名不能用"群 <号>"这种占位 —— 那说明群名没查到。
    # 用真的群号去查一遍，每个都该查得到。
    st = Store()
    from onebot import parse_event as _pe
    for gid, uid, segs, extra in DEMO_MESSAGES:
        payload = {"post_type": "message", "message_type": "group",
                   "group_id": int(gid), "user_id": int(uid), "self_id": 999,
                   "sender": {"nickname": "测试者"}, "message": segs,
                   "raw_message": "", "time": 1}
        if extra:
            payload["raw"] = extra
        e = _pe(payload, 999, dict(DEMO_MEMBERS))
        if e is not None:
            st.add(e)
    st.refresh_names(lambda g: DEMO_GROUPS.get(str(g)))
    placeholders = [(k, s["name"]) for k, s in st.ordered()
                    if s["name"].startswith("群 ")]
    check("★ 会话名都不是\"群 <号>\"占位（说明群号查得到）",
          not placeholders, repr(placeholders[:3]))
    check("会话名用上了真群名",
          any(s["name"] == DEMO_GROUPS["894859599"] for _k, s in st.ordered()),
          repr([s["name"] for _k, s in st.ordered()][:4]))

    # ---- 端到端：喂进去的消息走**真实**路径（parse_event → Store →
    #      message_lines），出来的东西必须能进 Tk、且不超宽 ----
    #
    #      ⚠️ 净化（clean）在 Store.add 里做，不在 parse_event 里。
    #      所以这里必须过 Store —— 只测 parse_event 会看到 emoji 还在，
    #      然后误以为"净化坏了"。
    from onebot import parse_event

    ok = 0
    nonbmp = []
    over = []
    for i, (gid, sender_uid, segs, extra) in enumerate(DEMO_MESSAGES):
        payload = {"post_type": "message", "message_type": "group",
                   "group_id": int(gid), "user_id": int(sender_uid),
                   "self_id": 999,
                   "sender": {"nickname": "测试者", "card": "测试者"},
                   "message": segs, "raw_message": "", "time": 1}
        if extra:
            payload["raw"] = extra
        e = parse_event(payload, 999, dict(DEMO_MEMBERS))
        if e is None:
            nonbmp.append((i, "parse_event 返回 None"))
            continue
        ok += 1

        st = Store()
        st.add(e)                       # ← 净化在这一步发生
        sess = st.get(("group", int(gid)))
        if sess is None:
            nonbmp.append((i, "没建出会话"))
            continue

        # 所有会进 Tk 的文本都要检查 —— 名称、正文、引用
        texts = [sess["name"]]
        for line, _is in message_lines(sess["msgs"], TEXT_W):
            texts.append(line)
        for t in texts:
            bad = [c for c in str(t) if ord(c) > 0xFFFF]
            if bad:
                nonbmp.append((i, repr(bad[:2])))
        for line, _is in message_lines(sess["msgs"], TEXT_W):
            if text_width(line) > TEXT_W:
                over.append((line, text_width(line)))

    check("★ 所有假消息都能被真实解析路径吃下", not nonbmp,
          repr(nonbmp[:3]))
    check("★ 过了 Store 之后没有非 BMP 字符能溜到 Tk", not nonbmp,
          repr(nonbmp[:3]))
    check("★ 所有假消息渲染出来都不超宽", not over, repr(over[:3]))
    check("确实全都过了一遍", ok == len(DEMO_MESSAGES),
          "%d/%d" % (ok, len(DEMO_MESSAGES)))


def test_refresh_view_dispatch():
    """刷新时按视图分派 —— "过一会儿刷新就退回主界面"那个 bug。

    根因：主循环里写成
        if view == CHAT: show_chat()
        else:            show_sessions()
    打开"发送消息"浮层时 view 是 VIEW_REPLY（不是 CHAT），一来新消息
    就掉进 else → 弹回会话列表、浮层也没了。群消息一来一回很快，
    表现就是"过一段时间刷新就退出去"。

    这里不实例化 GUI，只验证分派逻辑本身。
    """
    print("\n[刷新分派 —— 浮层不能被打断]")

    import app as appmod

    class FakeTerm(object):
        """记录调用了哪些方法，不碰真实 GUI。"""

        def __init__(self, view, cur=("group", 1)):
            self.view = view
            self.cur = cur
            self.menu_sel = 0
            self.calls = []
            self.box_shown = False

        # 把 QQTerm 的方法直接绑上来，测真实实现
        refresh_view = appmod.QQTerm.refresh_view
        _restore_box = appmod.QQTerm._restore_box

        def show_sessions(self):
            self.calls.append("show_sessions")
            self.view = appmod.VIEW_SESSIONS

        def show_chat(self):
            self.calls.append("show_chat")
            self.view = appmod.VIEW_CHAT

        def _draw_chat_body(self):
            self.calls.append("draw_body")

        def _clear(self, keep_box=False):
            self.calls.append("clear(keep_box=%s)" % keep_box)

        def _show_box(self, items, sel, y0=78):
            self.calls.append("show_box")
            self.box_shown = True

        def _set_status(self):
            self.calls.append("set_status")

        def _draw_input(self):
            self.calls.append("draw_input")

    V = appmod

    # 会话列表 → 刷新会话列表
    t = FakeTerm(V.VIEW_SESSIONS)
    t.refresh_view()
    check("列表页刷新调 show_sessions",
          "show_sessions" in t.calls, repr(t.calls))

    # 聊天页 → 重画正文，不切走
    t = FakeTerm(V.VIEW_CHAT)
    t.refresh_view()
    check("聊天页刷新重画正文",
          "draw_body" in t.calls, repr(t.calls))
    check("聊天页刷新后仍是聊天页",
          t.view == V.VIEW_CHAT, repr(t.view))

    # ★ 核心：发送消息浮层开着时，刷新**不能**退出聊天页
    t = FakeTerm(V.VIEW_REPLY)
    t.refresh_view()
    check("浮层(回复)刷新后视图不变",
          t.view == V.VIEW_REPLY, repr(t.view))
    check("浮层(回复)刷新不跳会话列表",
          "show_sessions" not in t.calls, repr(t.calls))
    check("浮层(回复)刷新后重新摆出来",
          t.box_shown and t.calls.count("show_box") >= 1, repr(t.calls))
    # ⚠️ 这条以前断言的是"调了 _clear(keep_box=True)"。后来**故意不调了**：
    # _clear 会把 9 行正文全置空，紧接的重画中间有一帧空屏 ——
    # 就是用户报的"新消息一来屏幕闪一下"。现在靠 _draw_chat_body
    # 自己"先写后清"（见 test_no_blank_flicker）。
    # 所以这里改断言"**没**清屏"，浮层照样要保住。
    check("浮层(回复)刷新时不整屏清空（清了就会闪）",
          "clear(keep_box=True)" not in t.calls, repr(t.calls))
    check("浮层(回复)刷新时浮层还在", t.box_shown, repr(t.calls))

    # ★ 菜单浮层同理
    t = FakeTerm(V.VIEW_MENU)
    t.refresh_view()
    check("浮层(菜单)刷新后视图不变",
          t.view == V.VIEW_MENU, repr(t.view))
    check("浮层(菜单)刷新不跳会话列表",
          "show_sessions" not in t.calls, repr(t.calls))
    check("浮层(菜单)刷新后重新摆出来", t.box_shown, repr(t.calls))

    # ★ 输入界面：打字打一半来了新消息，**绝对不能**被踢出去。
    #   群里消息一来一回很快，输入的内容还没发就没了是灾难性的。
    t = FakeTerm(V.VIEW_INPUT)
    t.refresh_view()
    check("输入界面刷新后仍是输入界面",
          t.view == V.VIEW_INPUT, repr(t.view))
    check("输入界面刷新不跳会话列表",
          "show_sessions" not in t.calls, repr(t.calls))
    check("输入界面刷新不跳聊天页",
          "show_chat" not in t.calls, repr(t.calls))
    check("输入界面刷新**不重画文本区和键盘**",
          "draw_input" not in t.calls, repr(t.calls))
    check("输入界面刷新只更新连接状态",
          "set_status" in t.calls, repr(t.calls))

    # 没有当前会话时应安全退回列表（不能崩）
    t = FakeTerm(V.VIEW_CHAT, cur=None)
    t.refresh_view()
    check("没有当前会话时安全退回列表",
          "show_sessions" in t.calls, repr(t.calls))

    # 反向：旧的分派逻辑会跳走 —— 证明这条测试确实能抓到 bug
    t = FakeTerm(V.VIEW_REPLY)
    if t.view == V.VIEW_CHAT:
        t.show_chat()
    else:
        t.show_sessions()
    check("（反向）旧逻辑确实会跳回列表",
          t.view == V.VIEW_SESSIONS, repr(t.view))


def main():
    print("=" * 46)
    print("M10_QQ 渲染 / 协议 离线测试")
    print("=" * 46)
    test_clean()
    test_fit_text()
    test_fit_text_by_size()
    test_group_name_overflow()
    test_wrap()
    test_message_lines()
    test_line_cache()
    test_drain_keeps_identity()
    test_voice_edge_cases()
    test_voice_state_machine()
    test_cand_width()
    test_keyboard_pages()
    test_clean_surrogate()
    test_sid_normalized()
    test_exit_btn_guard()
    test_reply_cache_invalidated()
    test_audioop_fallback()
    test_long_sender_name()
    test_store()
    test_extract_text()
    test_parse_event()
    test_end_to_end_no_nonbmp()
    test_group_names()
    test_reply()
    test_parse_reply_payload()
    test_at_names()
    test_keyboard_layout()
    test_cursor_placement()
    test_input_ui_show_hide()
    test_exit_button_dispatch()
    test_footer_hidden_in_input()
    test_seed_from_roster()
    test_friend_list_parsing()
    test_send_two_step()
    test_no_blank_flicker()
    test_demo_client()
    test_refresh_view_dispatch()

    print("\n" + "=" * 46)
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
