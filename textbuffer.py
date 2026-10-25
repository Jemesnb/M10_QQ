#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可编辑的文本缓冲区 —— 输入框的核心。

这个类**只认字符串和光标**，不碰屏幕、不碰网络、不碰麦克风。
所以它能完全离线测试，也所以它能同时喂三种输入源：

    键盘 ──┐
    语音 ──┼──▶ TextBuffer ──▶ 发送
    候选 ──┘

用户的要求是"语音输入的文字要可以用键盘改" —— 这句话在架构上的含义
就是：**语音不能直接发出去**，必须落进这个缓冲区，之后键盘能改它。
如果语音走的是"识别完直接 send"那条路，这个需求就没法满足。

⚠️ 两个容易踩的地方：

1. **光标按"字符"走，不按"字节"**。Python 3 的字符串下标天然就是字符，
   别自己去做 encode/decode —— 中文一个字算 1 个位置，不是 3 个。

2. **emoji 进来之前已经被 clean() 处理过了**（见 app.clean）。Tk 只认
   BMP 字符，辅助平面的字符会让 canvas 直接抛异常。缓冲区不做净化，
   但要求调用方送进来的已经是净化过的文本。
"""


class TextBuffer(object):
    """一个带光标和拼音组字状态的字符串。"""

    def __init__(self, max_len=500):
        # 一条 QQ 消息一般几十字，500 足够；主要防手滑一直按
        self.max_len = max_len
        self.text = ""
        self.cursor = 0          # 0 = 在第一个字符**前面**
        self.composing = ""      # 正在拼的拼音，"" = 没在组字
        self.candidates = []     # 当前候选字（composing 非空时才有）
        self.cand_syl = []       # 每个候选吃掉几个音节（见 set_candidates）

        # 撤销：只存一份快照，够用（用户要的是"刚才那下错了"，
        # 不是无限撤销栈 —— 512MB 的板子别乱存）
        self._undo = None

    # ---- 基本状态 ----

    def __len__(self):
        return len(self.text)

    def is_empty(self):
        return not self.text

    def composing_pending(self):
        """有没有还没落到正文的拼音。"""
        return bool(self.composing)

    def display(self):
        """给界面用的可见文本 —— **包含**正在拼的拼音。

        拼音还没选字时也要看得见，否则用户不知道自己敲了什么。
        """
        if not self.composing:
            return self.text
        return self.text[:self.cursor] + self.composing + self.text[self.cursor:]

    def cursor_display_pos(self):
        """光标在 display() 里的位置（候选栏要照着它画）。"""
        return self.cursor + len(self.composing)

    # ---- 撤销 ----

    def _snapshot(self):
        """改动**之前**存一份。只留最后一次。"""
        self._undo = (self.text, self.cursor)

    def can_undo(self):
        return self._undo is not None

    def undo(self):
        """撤销上一次改动。返回是否真的撤销了。"""
        if self._undo is None:
            return False
        self.text, self.cursor = self._undo
        self._undo = None
        self.cancel_compose()
        return True

    # ---- 插入 / 删除 ----

    def insert(self, s):
        """在光标处插入一段文本。超长就截掉，不报错。

        插入**之前**先把没选的拼音落顶上屏 —— 否则 "ni" 还挂着，
        用户中间插一段别的，拼音就跑到后面去了。
        """
        if not s:
            return
        if self.composing:
            self.commit_compose()
        self._snapshot()
        room = self.max_len - len(self.text)
        if room <= 0:
            return
        s = s[:room]
        self.text = self.text[:self.cursor] + s + self.text[self.cursor:]
        self.cursor += len(s)

    def backspace(self):
        """退格。有拼音在拼就先删拼音的一个字母（这是中文输入法的惯例）。"""
        if self.composing:
            self.composing = self.composing[:-1]
            if not self.composing:
                self.candidates = []
            return True
        if self.cursor <= 0:
            return False
        self._snapshot()
        self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
        self.cursor -= 1
        return True

    def delete_forward(self):
        """删除光标**后面**那个字符（Del 键）。"""
        if self.composing:
            # 中文组字时"删后面"没意义，当取消组字处理
            self.cancel_compose()
            return True
        if self.cursor >= len(self.text):
            return False
        self._snapshot()
        self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        return True

    def clear(self):
        self._snapshot()
        self.text = ""
        self.cursor = 0
        self.cancel_compose()

    # ---- 光标 ----

    def move_left(self):
        if self.composing:
            # 组字时左右键在候选之间跳更符合直觉，这里简化成移动拼音光标，
            # 先不做 —— 240px 屏上候选就用点击选
            return False
        if self.cursor <= 0:
            return False
        self.cursor -= 1
        return True

    def move_right(self):
        if self.composing:
            return False
        if self.cursor >= len(self.text):
            return False
        self.cursor += 1
        return True

    def move_home(self):
        self.cursor = 0

    def move_end(self):
        self.cursor = len(self.text)

    # ---- 拼音组字 ----

    def feed_key(self, ch):
        """敲了一个字母 → 进拼音缓冲，并刷新候选。

        返回候选列表（可能为空）。空表示这不是合法拼音，界面可以不管。
        """
        if not ch or not ch.isalpha() or not ch.isascii():
            return []
        self.composing += ch.lower()
        return self.refresh_candidates()

    def set_candidates(self, cands, syl_counts=None):
        """由拼音引擎算好候选后填进来（引擎不在这个类里，保持解耦）。

        syl_counts：每个候选**吃掉几个音节**，和 cands 一一对应。
        中文里一个字就是一个音节，所以默认取 len(word)。传进来是为了
        让 pick() 知道选了"你好"之后 "nihao" 已经用完了，而选了"你"
        还剩 "hao" 可以接着拼。
        """
        self.candidates = list(cands or [])
        if syl_counts is None:
            syl_counts = [len(w) for w in self.candidates]
        self.cand_syl = list(syl_counts)
        return self.candidates

    def refresh_candidates(self):
        """占位 —— 真正的候选由 PinyinIME 算，这里只保证接口一致。

        这样设计是为了让 TextBuffer 不依赖 pypinyin：测试时能不带
        任何数据跑，板子上没装词库也不会崩。
        """
        return self.candidates

    def pick(self, i, rest=None):
        """选中第 i 个候选，落进正文。

        rest：这个候选吃完之后**还剩的拼音**（调用方算好的）。
        ⚠️ 以前这里无条件 `self.composing = ""` —— 于是 "nihao" 选了
        "你" 之后 "hao" 直接被扔掉，只能一个字一个字打，两个词永远
        拼不成一句。现在剩下的音节留着，界面重算候选接着拼。

        ⚠️ 和 commit_compose 一样要受 max_len 约束（以前没有，
        正文满了再选字就会超长）。另外选字也要能撤销 —— 选错字
        比打错字更常见，没有快照的话撤销会跳到更早的操作上去。
        """
        if not self.composing or not (0 <= i < len(self.candidates)):
            return False
        word = self.candidates[i]
        room = self.max_len - len(self.text)
        if room <= 0:
            return False
        self._snapshot()
        word = word[:room]
        self.text = self.text[:self.cursor] + word + self.text[self.cursor:]
        self.cursor += len(word)
        # 剩下的音节接着拼（没有剩的就结束组字）
        self.composing = rest or ""
        self.candidates = []
        self.cand_syl = []
        return True

    def commit_compose(self):
        """把拼音原样上屏（用户不选字，直接敲了别的键）。

        比如用户打了 "ni" 又按了空格 —— 按输入法惯例应该把 "ni"
        当成英文字母落到正文，而不是丢掉。

        ⚠️ 这里必须和 insert() 一样受 max_len 约束。以前没管，
        正文已经到上限时再挂一段拼音，上屏后直接超长 —— 实测
        max_len=10 时能塞到 19 个字，而超出的部分 QQ 发不出去
        （NoneBot 那边报 message too long）。
        截断按**剩余空间**来，截不进去就丢弃并返回 False，
        别让界面以为成功上屏了。
        """
        if not self.composing:
            return False
        pending = self.composing
        self.composing = ""
        self.candidates = []
        room = self.max_len - len(self.text)
        if room <= 0:
            return False
        pending = pending[:room]
        self.text = self.text[:self.cursor] + pending + self.text[self.cursor:]
        self.cursor += len(pending)
        return True

    def cancel_compose(self):
        """丢掉正在拼的拼音（Esc）。"""
        self.composing = ""
        self.candidates = []

    # ---- 语音 ----

    def append_voice(self, text):
        """语音识别的结果落进缓冲区。

        ⚠️ 这是"语音输入能用键盘改"的落点 —— 走的是和键盘**同一条**
        插入路径，所以之后退格、左右移动、撤销都一样好使。
        别在这儿另写一套直接拼字符串的逻辑。
        """
        if not text:
            return False
        self.insert(text)
        return True

    # ---- 给界面用的快照 ----

    def state(self):
        """一次性把界面要的东西都取出来，省得界面上多次调用。"""
        return {
            "text": self.display(),
            "cursor": self.cursor_display_pos(),
            "composing": self.composing,
            "candidates": list(self.candidates),
        }

    def __repr__(self):
        return "<TextBuffer %r cur=%d%s>" % (
            self.text, self.cursor,
            " 拼=%s" % self.composing if self.composing else "")
