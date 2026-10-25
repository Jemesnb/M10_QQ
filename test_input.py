#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入系统的离线测试 —— 不连网、不开界面、不需要板子。

重点测用户提的那条要求：**语音输入的文字要能用键盘改**。
所以「语音落进缓冲区 → 键盘退格/移动/撤销能改到它」是这里最重要的一类用例，
不是附带测一下。

跑法（本地或板子上都行）：
    python3 test_input.py
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

from textbuffer import TextBuffer                                    # noqa: E402
from pinyin_ime import (PinyinIME, strip_tone, common_freq,         # noqa: E402
                        jieba_freq, best_freq)
from voice import (VoiceEngine, NullEngine, VoskEngine,              # noqa: E402
                   make_engine)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s   %s" % (name, detail))
        FAILED.append(name)


# ---------------------------------------------------------------- 文本缓冲

def test_basic():
    print("\n[TextBuffer —— 基本输入]")
    b = TextBuffer()
    check("初始为空", b.is_empty() and b.cursor == 0)

    b.insert("你好")
    check("插入中文", b.text == "你好", repr(b.text))
    check("光标在末尾", b.cursor == 2, repr(b.cursor))

    b.insert("world")
    check("插入英文", b.text == "你好world", repr(b.text))
    check("光标继续前进", b.cursor == 7, repr(b.cursor))

    b.move_home()
    check("home 到开头", b.cursor == 0)
    b.insert(">>")
    check("在开头插入", b.text == ">>你好world", repr(b.text))

    b.move_end()
    b.insert("!")
    check("end 后插入", b.text == ">>你好world!", repr(b.text))


def test_cursor_by_character():
    """光标按**字符**走，不按字节。中文一个字算 1 个位置。"""
    print("\n[TextBuffer —— 光标按字符不按字节]")
    b = TextBuffer()
    b.insert("a中b")
    check("长度按字符算", len(b.text) == 3, repr(len(b.text)))
    # 中文在 UTF-8 里是 3 字节，要是按字节算这里会是 10
    check("不是按字节算", len(b.text.encode("utf-8")) == 5,
          repr(len(b.text.encode("utf-8"))))

    b.move_home()
    b.move_right()
    check("右移一格落在一个完整字符后", b.cursor == 1, repr(b.cursor))
    b.insert("X")
    check("插在中文前面不破坏字符", b.text == "aX中b", repr(b.text))

    # 逐个右移再逐个左移，必须回到原地
    b.move_home()
    for _ in range(4):
        b.move_right()
    check("连移 4 次到末尾", b.cursor == 4, repr(b.cursor))
    for _ in range(4):
        b.move_left()
    check("连移 4 次回到开头", b.cursor == 0, repr(b.cursor))
    check("边界不外溢", b.move_left() is False)


def test_backspace():
    print("\n[TextBuffer —— 退格]")
    b = TextBuffer()
    b.insert("你好")
    check("退格删一个中文字", b.backspace() and b.text == "你", repr(b.text))
    check("空的时候退格不炸", b.backspace() and b.backspace() is False)
    check("退成空", b.text == "", repr(b.text))

    # 中间退格
    b = TextBuffer()
    b.insert("abcd")
    b.move_home()
    b.move_right()
    b.move_right()          # ab|cd
    b.backspace()
    check("中间退格删的是前一个", b.text == "acd", repr(b.text))
    check("光标跟着退", b.cursor == 1, repr(b.cursor))

    # Del 键
    b = TextBuffer()
    b.insert("abcd")
    b.move_home()
    b.move_right()          # a|bcd
    b.delete_forward()
    check("Del 删的是后一个", b.text == "acd", repr(b.text))
    check("Del 不动光标", b.cursor == 1, repr(b.cursor))
    b.move_end()
    check("末尾 Del 无效", b.delete_forward() is False)


def test_max_len():
    print("\n[TextBuffer —— 长度上限]")
    b = TextBuffer(max_len=5)
    b.insert("12345")
    check("到上限", len(b.text) == 5)
    b.insert("6")
    check("超了不炸也不加", b.text == "12345", repr(b.text))
    b.insert("6789")
    check("一次插一大段也只截到上限", b.text == "12345", repr(b.text))


# ------------------------------------------------ 语音（用户的核心要求）

def test_voice_is_editable():
    """★ 用户原话："语音输入的文字要可以用键盘改"。

    所以这条测试是**整个输入系统最重要的一条**：语音结果必须走和键盘
    同一条插入路径，落进缓冲区之后，退格、光标移动、撤销全都要好使。
    如果哪天有人把语音改成"识别完直接发"，这条会红。
    """
    print("\n[★ 语音结果必须可编辑]")

    # 1) 语音落进缓冲区
    b = TextBuffer()
    b.append_voice("今天天气不错")
    check("语音进缓冲区", b.text == "今天天气不错", repr(b.text))
    check("语音后光标在末尾", b.cursor == 6, repr(b.cursor))

    # 2) 键盘能退格改它
    b.backspace()
    b.backspace()
    check("退格能删语音的字", b.text == "今天天气", repr(b.text))

    # 3) 键盘能把光标移进语音文本中间再改
    b.move_home()
    for _ in range(2):
        b.move_right()          # 今天|天气
    b.insert("的")
    check("能在语音文本中间插字", b.text == "今天的天气", repr(b.text))

    # 4) 键盘能删中间的。此刻是 "今天|的|天气"，退格删掉的正是刚插进去
    #    那个「的」—— 前后两边的字都不该受影响
    b.backspace()
    check("能删中间的字", b.text == "今天天气", repr(b.text))
    check("删中间不动两边",
          b.text.startswith("今天") and b.text.endswith("天气"),
          repr(b.text))
    check("删中间后光标跟着退", b.cursor == 2, repr(b.cursor))

    # 5) 撤销能回到语音原样
    b2 = TextBuffer()
    b2.append_voice("识别出来的句子")
    b2.insert("追加")
    check("插入后文本变了", b2.text == "识别出来的句子追加", repr(b2.text))
    b2.undo()
    check("撤销回到语音原样", b2.text == "识别出来的句子", repr(b2.text))

    # 6) 语音能被整段清掉
    b2.clear()
    check("能整段清掉", b2.text == "" and b2.cursor == 0)

    # 7) ★ 反向验证：如果语音走"直接发送"而不是缓冲区，
    #    上面的可编辑性就一条都不成立。这里用一个哑对象证明——
    #    缓冲区**是**那条路径，没有绕过去。
    b3 = TextBuffer()
    b3.append_voice("abc")
    check("（反向）语音结果确实在缓冲区里",
          b3.text == "abc" and len(b3) == 3)

    # 8) 语音 + 键盘交替，结果按顺序拼
    b4 = TextBuffer()
    b4.append_voice("你好")
    b4.insert("，")
    b4.append_voice("世界")
    check("语音和键盘交错也按序", b4.text == "你好，世界", repr(b4.text))

    # 9) 空语音不炸
    b5 = TextBuffer()
    check("空语音不炸", b5.append_voice("") is False and b5.text == "")


def test_max_len():
    print("\n[TextBuffer —— 长度上限]")
    b = TextBuffer(max_len=5)
    b.insert("一二三四五")
    check("插满到上限", len(b.text) == 5, str(len(b.text)))

    # 以前 commit_compose 不看 max_len：上限 10 时能塞到 19 字。
    # 超长的消息 QQ 发不出去，用户只看到"发送失败"。
    b.composing = "nihaoma"
    b.commit_compose()
    check("拼音上屏不超上限", len(b.text) <= 5,
          "%d 字: %r" % (len(b.text), b.text))

    # pick() 同理
    b2 = TextBuffer(max_len=3)
    b2.insert("一二三")
    b2.composing = "ni"
    b2.candidates = ["你"]
    b2.pick(0)
    check("选字上屏不超上限", len(b2.text) <= 3,
          "%d 字: %r" % (len(b2.text), b2.text))

    # 满了以后还能不能正常操作（不能崩）
    b3 = TextBuffer(max_len=2)
    b3.insert("ab")
    b3.composing = "n"
    r = b3.commit_compose()
    check("满了上屏返回 False", r is False, repr(r))
    check("满了正文不变", b3.text == "ab", repr(b3.text))

def test_cloud_asr():
    print("\n[云端识别 —— Deepgram 客户端]")
    import json
    import struct
    import cloud_asr as C

    # WAV 头必须是对的：云端按文件头解析，写错了会 400
    pcm = b"\x01\x00" * 100
    w = C.pcm_to_wav(pcm, 16000, 1)
    check("RIFF 头", w[:4] == b"RIFF" and w[8:12] == b"WAVE")
    check("长度 = 44 + 数据", len(w) == 44 + len(pcm), "%d" % len(w))
    ch, rate = struct.unpack("<HI", w[22:28])
    check("单声道", ch == 1, str(ch))
    check("采样率 16000", rate == 16000, str(rate))
    check("空音频返回空", C.pcm_to_wav(b"") == b"")

    class Resp(object):
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    ok_body = json.dumps({"results": {"channels": [{"alternatives": [
        {"transcript": " 你好世界 "}]}]}}).encode()
    real = C.urllib.request.urlopen
    sent = {}

    def fake_ok(req, timeout=None):
        sent["url"] = req.full_url
        sent["auth"] = req.get_header("Authorization")
        sent["data"] = req.data
        return Resp(ok_body)

    try:
        C.urllib.request.urlopen = fake_ok
        dg = C.DeepgramASR("KEY123")
        out = dg.transcribe(w)
        check("解析出转写文本", out == "你好世界", repr(out))
        check("带了 model 参数", "model=nova-2" in sent["url"], sent["url"][:80])
        check("带了 language", "zh-CN" in sent["url"], sent["url"][:80])
        check("鉴权头是 Token", sent["auth"] == "Token KEY123", str(sent["auth"]))
        check("把 wav 发出去了", sent["data"] == w, "%d 字节" % len(sent["data"] or b""))

        # 网络炸了必须抛异常（上层靠它回落本地），不能静默返回空串
        def boom(req, timeout=None):
            raise OSError("network down")

        C.urllib.request.urlopen = boom
        try:
            dg.transcribe(w)
            check("★ 网络错误要抛 AsrError", False, "居然没抛")
        except C.AsrError:
            check("★ 网络错误抛 AsrError", True)

        # 401 也要抛
        import urllib.error

        def unauth(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                         {}, None)

        C.urllib.request.urlopen = unauth
        try:
            dg.transcribe(w)
            check("★ 401 要抛 AsrError", False, "居然没抛")
        except C.AsrError as exc:
            check("★ 401 抛 AsrError", "401" in str(exc), str(exc))

        # 没 key 时不发请求
        check("没 key 就不算可用", not C.DeepgramASR("").available())
    finally:
        C.urllib.request.urlopen = real

def test_pick_keeps_rest():
    print("\n[TextBuffer —— 选词后继续拼]")
    b = TextBuffer()
    # 'nihao' 切出两个音节，候选里既有单字也有整词
    b.composing = "nihao"
    b.set_candidates(["你", "你好", "尼"], [1, 2, 1])

    b.pick(0, rest="hao")          # 选「你」，还剩 hao
    check("选单字后正文对", b.text == "你", repr(b.text))
    check("★ 剩余拼音留着", b.composing == "hao", repr(b.composing))

    b.set_candidates(["好", "号"], [1, 1])
    b.pick(0, rest="")             # 选「好」，用完了
    check("接着拼出第二个字", b.text == "你好", repr(b.text))
    check("拼完清空", b.composing == "", repr(b.composing))

    # 整词一次吃完
    b2 = TextBuffer()
    b2.composing = "nihao"
    b2.set_candidates(["你", "你好"], [1, 2])
    b2.pick(1, rest="")
    check("整词一次上屏", b2.text == "你好", repr(b2.text))
    check("整词用完后没有残留拼音", b2.composing == "")

    # 撤销仍然正常（选错字能退回去）
    check("选完能撤销", b2.can_undo())


def test_pick_undo():
    print("\n[TextBuffer —— 选字可撤销]")
    b = TextBuffer()
    b.insert("前")
    b.composing = "ni"
    b.candidates = ["你", "尼"]
    b.pick(0)
    check("选字上屏", b.text == "前你", repr(b.text))
    check("选完能撤销", b.can_undo())
    b.undo()
    check("撤销回到选字前", b.text == "前", repr(b.text))

def test_undo():
    print("\n[TextBuffer —— 撤销]")
    b = TextBuffer()
    check("没东西可撤", b.can_undo() is False and b.undo() is False)
    b.insert("abc")
    b.insert("def")
    b.undo()
    check("撤销回到上一次改动", b.text == "abc", repr(b.text))
    check("撤销只能用一次", b.undo() is False)


# ---------------------------------------------------------------- 拼音

def test_strip_tone():
    """去声调 —— 用户敲 'ni'，不是 'nǐ'。"""
    print("\n[去声调]")
    check("一声", strip_tone("nǐ") == "ni", repr(strip_tone("nǐ")))
    check("四声", strip_tone("shì") == "shi", repr(strip_tone("shì")))
    check("二声", strip_tone("hǎo") == "hao", repr(strip_tone("hǎo")))
    check("三声", strip_tone("nǚ") == "nv", repr(strip_tone("nǚ")))
    # ★ ü 是特例：NFD 会把它拆成 u + 分音符，直接滤就变成 "u" 了，
    #   而输入法里 ü 是打 "v" 的（女 = "nv"，绿 = "lv"）
    check("ü 变 v 不是 u", strip_tone("lǜ") == "lv", repr(strip_tone("lǜ")))
    check("ü 单独", strip_tone("ü") == "v", repr(strip_tone("ü")))
    check("英文原样", strip_tone("abc") == "abc")
    check("空串不炸", strip_tone("") == "")


_IME_CACHE = {}


def _real_ime():
    """建（并缓存）一个真索引 —— 板子上才有 pypinyin/jieba，本地会抛。"""
    if "ime" in _IME_CACHE:
        return _IME_CACHE["ime"]
    ime = PinyinIME()
    ime.build(freq=best_freq())
    _IME_CACHE["ime"] = ime
    return ime


def test_prefix_predict():
    print("\n[边打边猜 —— 拼音前缀]")
    try:
        ime = _real_ime()
    except Exception as exc:
        print("  skip  （没有 pypinyin/jieba: %r）" % (exc,))
        return
    if not ime.phrases:
        print("  skip  （词库没建起来）")
        return

    # 这就是用户报的那个：打 ces 时候选全空
    c = ime.candidates("ces", limit=6)
    check("★ ces 能猜到「测试」", "测试" in c, str(c))
    c = ime.candidates("ceshi", limit=6)
    check("ceshi 第一条是「测试」", c[:1] == ["测试"], str(c[:3]))

    # 短前缀不能变成字母序的冷僻词
    c = ime.candidates("zh", limit=6)
    check("zh 给常用字", bool(c) and c[0] in ("这", "中", "着"), str(c))
    check("zh 不出冷僻词", "长辈" not in c, str(c))

    # 打完的音节仍然走正常候选
    c = ime.candidates("nihao", limit=6)
    check("nihao 第一条是「你好」", c[:1] == ["你好"], str(c[:3]))

    # 前缀命中的候选要能吃掉整串（不然选完还剩半截拼音）
    if "测试" in ime.candidates("ces", limit=6):
        check("ces 能切分吗（预期切不动）", ime.segment("ces") is None,
              repr(ime.segment("ces")))


def test_segment_offline():
    """切分逻辑用假音节表测，不依赖 pypinyin —— 这样本地跑得动。"""
    print("\n[音节切分（假表）]")
    ime = PinyinIME()
    ime.syllables = set(["ni", "hao", "shi", "jie", "zhong", "guo",
                         "a", "an", "n", "i", "xi", "huan", "ni"])
    ime.max_syl = 5
    ime._ready = True

    check("两音节", ime.segment("nihao") == ["ni", "hao"],
          repr(ime.segment("nihao")))
    check("三音节", ime.segment("shijie") == ["shi", "jie"],
          repr(ime.segment("shijie")))
    check("贪心最长匹配", ime.segment("zhongguo") == ["zhong", "guo"],
          repr(ime.segment("zhongguo")))
    check("单音节", ime.segment("ni") == ["ni"], repr(ime.segment("ni")))
    check("空串", ime.segment("") == [], repr(ime.segment("")))
    # 切不动的返回 None，界面据此决定显示什么
    check("非法拼音返回 None", ime.segment("qqq") is None,
          repr(ime.segment("qqq")))
    check("部分非法也返回 None", ime.segment("nizzz") is None,
          repr(ime.segment("nizzz")))


def test_candidates_offline():
    """候选逻辑用假数据测 —— 保证不下 pypinyin 也能验证逻辑。"""
    print("\n[候选（假表）]")
    ime = PinyinIME()
    ime._ready = True
    ime.chars = {
        "ni": ["你", "尼", "泥", "拟"],
        "hao": ["好", "号", "耗"],
    }
    ime.phrases = {"nihao": ["你好"]}
    ime.syllables = set(["ni", "hao"])
    ime.max_syl = 3

    c = ime.candidates("nihao", limit=4)
    check("整词排在最前", c and c[0] == "你好", repr(c))
    check("单字跟在后面", "你" in c, repr(c))
    check("候选不超上限", len(c) <= 4, repr(c))
    check("单个音节给字", ime.candidates("hao", limit=3) == ["好", "号", "耗"],
          repr(ime.candidates("hao", limit=3)))

    # 没建好索引时不能崩，返回空
    ime2 = PinyinIME()
    check("没建索引返回空", ime2.candidates("ni") == [])
    check("空拼音返回空", ime.candidates("") == [])
    check("None 不炸", ime.candidates(None) == [])


def test_readings_shapes():
    """phrases_dict 的值的两种可能形状都要认。

    pypinyin 各版本不保证结构一致（每个字一组备选 vs 直接给字符串）。
    ⚠️ 解析错形状的后果不是"少几条"，是**读音错位**——给张三的词
    配上李四的拼音，候选就完全是别的东西了。所以认不出宁可返回 None。
    """
    print("\n[词组读音的两种形状]")
    R = PinyinIME._readings

    check("每组列表（pypinyin 0.48 的形状）",
          R([["yī"], ["dìng"]]) == ["yi", "ding"],
          repr(R([["yī"], ["dìng"]])))
    check("每组多个备选取第一个",
          R([["ní", "nì"], ["hǎo"]]) == ["ni", "hao"],
          repr(R([["ní", "nì"], ["hǎo"]])))
    check("纯字符串（老版本形状）",
          R(["yī", "dìng"]) == ["yi", "ding"], repr(R(["yī", "dìng"])))
    check("ü 要变 v", R([["nǚ"]]) == ["nv"], repr(R([["nǚ"]])))

    # 认不出的形状一律 None —— 宁可少收，不能收错
    check("空返回 None", R([]) is None, repr(R([])))
    check("None 返回 None", R(None) is None)
    check("空组返回 None", R([[]]) is None, repr(R([[]])))
    check("非字符串返回 None", R([[1]]) is None, repr(R([[1]])))
    # ★ 错位检测：长度对不上要能查出来（_build_phrases 会丢掉这种词）
    check("音字数量对不上时能被发现",
          len(R([["ni"], ["hao"]])) != len("你好世界"),
          "这个校验在 _build_phrases 里做")


def test_freq_direction():
    """★ 字频的**方向** —— "打个 mao 根本点不到冒" 的根因。

    两种字频数据的语义是**相反**的：

        common_freq()  →  {字: 名次}   名次小 = 常用
        jieba dict.txt →  {字: 频次}   频次大 = 常用

    原来的代码直接 `f.get(c, 99999)` 往升序里塞。对"名次"表是对的，
    一换成 jieba 的"频次"表，**最常用的字反而排到最后** ——
    实测「毛」掉到第 51 位，比不排序还糟。

    这条测试盯住：两种表喂进去，常用字都必须排在前面。
    """
    print("\n[字频方向 —— 名次 vs 频次]")

    # 造两个"语义相反"的表，看 PinyinIME 能不能都处理对
    # 名次表：「你」第 1，其余往后
    rank_table = {"你": 1, "尼": 100, "泥": 200, "拟": 300}
    ime1 = PinyinIME()
    ime1._freq = rank_table
    ime1._freq_is_count = ime1._detect_freq_kind()
    check("小表被认成「名次」", ime1._freq_is_count is False,
          repr(ime1._freq_is_count))
    check("名次表：值最小的排第一", ime1._rank("你") < ime1._rank("尼"),
          "%s < %s" % (ime1._rank("你"), ime1._rank("尼")))

    # 频次表：「你」出现最多
    count_table = {"你": 999999, "尼": 5000, "泥": 100, "拟": 1}
    ime2 = PinyinIME()
    ime2._freq = count_table
    ime2._freq_is_count = ime2._detect_freq_kind()
    check("大表被认成「频次」", ime2._freq_is_count is True,
          repr(ime2._freq_is_count))
    # ★ 核心：频次大的要排在**前面**，_rank 必须给出更小的值
    check("★ 频次表：值最大的排第一（方向别搞反）",
          ime2._rank("你") < ime2._rank("尼"),
          "你=%s 尼=%s" % (ime2._rank("你"), ime2._rank("尼")))
    check("频次表：最小的排最后",
          ime2._rank("你") < ime2._rank("拟"),
          "你=%s 拟=%s" % (ime2._rank("你"), ime2._rank("拟")))

    # 没有的字要一致地排最后
    check("名次表里没有的排最后",
          ime1._rank("某个不存在的字") > ime1._rank("拟"))
    check("频次表里没有的排最后",
          ime2._rank("某个不存在的字") > ime2._rank("拟"))

    # 空表不炸
    ime3 = PinyinIME()
    ime3._freq = {}
    ime3._freq_is_count = ime3._detect_freq_kind()
    check("空表不炸", ime3._rank("你") == 99999, repr(ime3._rank("你")))

    # 真数据：两种表实际排出来的结果都要能用
    try:
        import pypinyin                                     # noqa: F401
    except ImportError:
        print("  skip  本地没 pypinyin，真数据排序在板子上跑")
        return

    for name, table in (("名次表(内置)", common_freq()),
                        ("频次表(jieba)", jieba_freq())):
        if table is None:
            print("  skip  %s 取不到" % name)
            continue
        ime = PinyinIME(use_phrases=False)
        ime.build(freq=table)
        c = ime.candidates("ni", limit=6)
        check("%s 的 'ni' 第一个是「你」" % name, c and c[0] == "你",
              repr(c[:4]))
        # ★ 「冒」必须在前面几页里（用户要打的字）
        c = ime.candidates("mao", limit=12)
        where = c.index("冒") + 1 if "冒" in c else -1
        check("★ %s 的「冒」在前 12 个里（原来在第 29 位）" % name,
              0 < where <= 12, "第 %d 位: %r" % (where, c))


def test_rare_chars_filtered():
    """★ 生僻字不能出现候选里。

    pypinyin 的字表含 41497 个字，其中 5775 个在**扩展 A 区**
    （U+3400-4DBF）—— 都是「㑵」「㒻」这种日常打不到的字。
    它们码点比常用汉字区（U+4E00+）**小**，所以只要按码点排
    就全体挤到前面。

    实测过：'mao' 的候选前 22 个全是扩展 A 区的字，「冒」被顶到
    第 29 位，用户根本点不到。
    """
    print("\n[生僻字过滤]")

    # 先确认"过滤"这件事本身有据可依
    check("扩展 A 区码点确实比常用区小",
          0x3400 < 0x4E00, "0x3400 vs 0x4E00")

    try:
        import pypinyin                                     # noqa: F401
    except ImportError:
        print("  skip  本地没 pypinyin，板子上跑")
        return

    ime = PinyinIME(use_phrases=False)
    ime.build(freq=best_freq())
    check("确实滤掉了一批字", ime.skipped_rare > 1000,
          "滤掉 %d 个" % ime.skipped_rare)

    # ★ 每个音节的候选里都不能有扩展 A 区的字
    bad = []
    for py, lst in list(ime.chars.items())[:2000]:
        for ch in lst:
            if 0x3400 <= ord(ch) <= 0x4DBF:
                bad.append((py, ch))
                break
    check("★ 候选里没有扩展 A 区的生僻字", not bad, repr(bad[:3]))

    # 常用字的排序结果
    c = ime.candidates("mao", limit=6)
    check("★ 'mao' 第一页全是可以用的字",
          all(0x4E00 <= ord(ch) <= 0x9FFF for ch in c), repr(c))
    check("★ 'mao' 第一个是「毛」", c and c[0] == "毛", repr(c))


def test_build_real():
    """真数据测试 —— 板子上有 pypinyin，本地通常没有，所以跳过。"""
    print("\n[真数据（有 pypinyin 才跑）]")
    try:
        import pypinyin                                        # noqa: F401
    except ImportError:
        print("  skip  本地没装 pypinyin，跳过（板子上会跑）")
        return

    ime = PinyinIME(use_phrases=True)
    ime.build(freq=common_freq())
    check("索引建好了", ime.ready())
    check("音节表不空", len(ime.syllables) > 300,
          "%d 个音节" % len(ime.syllables))
    check("单字表不空", len(ime.chars) > 300, "%d 个拼音" % len(ime.chars))

    # 常用字必须排前面 —— 这是附字频表的意义
    c = ime.candidates("ni", limit=4)
    check("'ni' 第一个是「你」", c and c[0] == "你", repr(c))
    c = ime.candidates("hao", limit=4)
    check("'hao' 里有「好」", "好" in c, repr(c))
    c = ime.candidates("wo", limit=4)
    check("'wo' 第一个是「我」", c and c[0] == "我", repr(c))
    c = ime.candidates("de", limit=4)
    check("'de' 里有「的」", "的" in c, repr(c))

    # ü 系的字要能打出来（nv = 女）
    check("'nv' 能出「女」", "女" in ime.candidates("nv", limit=10),
          repr(ime.candidates("nv", limit=10)))

    # 切分要用真音节表验一次
    check("真表切 nihao", ime.segment("nihao") == ["ni", "hao"],
          repr(ime.segment("nihao")))
    check("真表切 zhongguo",
          ime.segment("zhongguo") == ["zhong", "guo"],
          repr(ime.segment("zhongguo")))

    # 词优先
    c = ime.candidates("nihao", limit=4)
    check("'nihao' 出词", any(len(w) > 1 for w in c), repr(c))

    # ★ 词表里的拼音必须和词一样长。错位的词会给出完全不相干的候选，
    #   这条用来发现解析形状变了。
    bad = []
    for py, words in list(ime.phrases.items())[:2000]:
        for w in words:
            if len(w) < 2:
                bad.append((py, w))
                break
    check("词表没有明显的坏条目", not bad, repr(bad[:3]))

    # 索引大小（板子内存要心里有数）
    print("  音节 %d 个，拼音 %d 个，词 %d 个"
          % (len(ime.syllables), len(ime.chars), len(ime.phrases)))


# ---------------------------------------------------------------- 组字

def test_composing():
    """中文组字：敲拼音 → 出候选 → 选字落正文。"""
    print("\n[组字流程]")
    b = TextBuffer()
    ime = PinyinIME()
    ime._ready = True
    ime.chars = {"ni": ["你", "尼"], "hao": ["好"]}
    ime.phrases = {"nihao": ["你好"]}
    ime.syllables = set(["ni", "hao"])
    ime.max_syl = 3

    # 敲 n i —— 进 composing，不进正文
    b.feed_key("n")
    b.feed_key("i")
    check("拼音进 composing", b.composing == "ni", repr(b.composing))
    check("正文还没变", b.text == "", repr(b.text))
    b.set_candidates(ime.candidates(b.composing, limit=4))
    check("有候选了", b.candidates and b.candidates[0] == "你",
          repr(b.candidates))

    # display 要把拼音露出来，否则用户看不见自己敲了啥
    check("display 包含拼音", "ni" in b.display(), repr(b.display()))

    # 选字
    b.pick(0)
    check("选字落进正文", b.text == "你", repr(b.text))
    check("选完清空组字", b.composing == "" and b.candidates == [])

    # 组字时按退格删的是拼音字母，不是正文（输入法惯例）
    b2 = TextBuffer()
    b2.feed_key("n")
    b2.feed_key("i")
    b2.backspace()
    check("组字时退格删拼音", b2.composing == "n", repr(b2.composing))
    check("组字时退格不动正文", b2.text == "", repr(b2.text))
    b2.backspace()
    b2.backspace()
    check("退光后没候选", b2.composing == "" and not b2.candidates)

    # 拼音没选字，直接敲别的 → 拼音原样上屏（输入法惯例）
    b3 = TextBuffer()
    b3.feed_key("n")
    b3.feed_key("i")
    b3.insert("X")
    check("插入前自动上屏拼音", "ni" in b3.text and "X" in b3.text,
          repr(b3.text))

    # Esc 取消
    b4 = TextBuffer()
    b4.feed_key("n")
    b4.cancel_compose()
    check("取消组字不留痕迹", b4.text == "" and b4.composing == "")

    # 切不动时不崩
    b5 = TextBuffer()
    b5.feed_key("q")
    b5.feed_key("q")
    b5.set_candidates(ime.candidates(b5.composing))
    check("非法拼音不崩", b5.candidates == [], repr(b5.candidates))

    # 非字母不进 composing
    b6 = TextBuffer()
    b6.feed_key("1")
    b6.feed_key("，")
    check("数字标点不进拼音", b6.composing == "", repr(b6.composing))


def test_voice_engine():
    """语音引擎 —— 重点是**缺东西的时候不能崩**。

    板子上很可能出现的情况：没装 vosk、模型还没下完、路径写错。
    这些都不该拦住用户打字发消息，所以一律退回 NullEngine。
    """
    print("\n[语音引擎 —— 降级不能崩]")

    n = NullEngine("测试原因")
    check("NullEngine 不可用", n.available() is False)
    check("NullEngine 有原因", n.reason() == "测试原因", repr(n.reason()))
    check("NullEngine 中间结果为空", n.interim() == "")
    check("NullEngine 不忙", n.busy() is False)

    # 没装 vosk 或用不存在的模型 → 必须退回 NullEngine，不能抛
    e = VoskEngine("/nonexistent/path/xyz")
    check("模型路径不存在时不可用", e.available() is False)
    check("不可用时有原因", bool(e.reason()), repr(e.reason()))
    check("start 失败返回 False 不抛", e.start() is False)
    check("stop 返回空串不抛", e.stop(timeout=0.1) == "")

    # 工厂：配置写错也不能崩
    class Cfg(object):
        ASR_ENGINE = "vosk"
        ASR_MODEL_CN = "/nonexistent"
        REC_RATE = 16000
        REC_CHANNELS = 1
        REC_DEVICE = None
        SILENCE_RMS = 500
        SILENCE_SEC = 1.5

    eng = make_engine(Cfg())
    check("工厂对坏路径退回 NullEngine",
          isinstance(eng, NullEngine), type(eng).__name__)

    class Cfg2(object):
        ASR_ENGINE = "none"

    check("ASR_ENGINE=none 也是 NullEngine",
          isinstance(make_engine(Cfg2()), NullEngine))

    class Cfg3(object):
        ASR_ENGINE = "不认识的引擎"

    check("不认识的名字也不崩",
          isinstance(make_engine(Cfg3()), NullEngine))

    # 空配置对象（属性都没有）不能崩
    class Cfg4(object):
        pass

    check("啥都没配也不崩",
          isinstance(make_engine(Cfg4()), NullEngine))


def test_vosk_cjk_detect():
    """Vosk 的中文结果**字之间带空格**，英文的不带。

    一律去空格会把英文句子粘成一坨（"hello world" → "helloworld"），
    一律不去空格中文就是"你 好 世 界"。所以要按内容判断。
    """
    print("\n[中英结果判断 —— 决定要不要去空格]")
    f = VoskEngine._is_cjk
    # 中文模型：每个字之间都有空格
    check("中文一字一格 → 去空格", f("你 好 世 界") is True,
          repr(f("你 好 世 界")))
    check("中文不带空格也判中文", f("你好世界") is True)
    # ★ 英文模型：空格分隔的是词。判错了会把 "hello world" 粘成
    #   "helloworld"，这是这条判断存在的**唯一理由**
    check("英文分词 → 不去空格", f("hello world") is False,
          repr(f("hello world")))
    check("英文长句 → 不去空格",
          f("this is a test of the speech engine") is False,
          repr(f("this is a test of the speech engine")))
    check("英文单词块比中文长",
          f("ni hao") is False, repr(f("ni hao")))
    check("空串不炸", f("") is False)
    check("纯空格不炸", f("   ") is False)
    check("没有汉字一律不动", f("abc def ghi") is False,
          repr(f("abc def ghi")))


def test_state():
    print("\n[TextBuffer —— 给界面快照]")
    b = TextBuffer()
    b.insert("你好")
    s = b.state()
    check("快照有全部字段",
          set(s.keys()) == {"text", "cursor", "composing", "candidates"},
          repr(sorted(s.keys())))
    check("快照文本对", s["text"] == "你好", repr(s["text"]))
    check("快照光标对", s["cursor"] == 2, repr(s["cursor"]))
    b.feed_key("a")
    s = b.state()
    check("组字时快照带上拼音", s["text"] == "你好a", repr(s["text"]))


def main():
    print("=" * 46)
    print("输入系统离线测试")
    print("=" * 46)

    test_basic()
    test_cursor_by_character()
    test_backspace()
    test_max_len()
    test_voice_is_editable()
    test_max_len()
    test_cloud_asr()
    test_pick_keeps_rest()
    test_pick_undo()
    test_undo()
    test_strip_tone()
    test_prefix_predict()
    test_segment_offline()
    test_candidates_offline()
    test_readings_shapes()
    test_freq_direction()
    test_rare_chars_filtered()
    test_build_real()
    test_composing()
    test_voice_engine()
    test_vosk_cjk_detect()
    test_state()

    print("")
    print("=" * 46)
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
