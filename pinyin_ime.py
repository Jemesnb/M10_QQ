#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拼音 → 汉字的候选引擎。

数据全部来自**板子上已经装好的 pypinyin**，不新增任何依赖：

    pinyin_dict   41497 个字：码点 -> 'ní,nì'（带声调，多音字用逗号隔开）
    phrases_dict  45471 个词：(字,字,...) -> [[拼音...], [拼音...], ...]

⚠️ pypinyin 本身是**反方向**的（汉字 → 拼音）。这里把它倒过来用，
建成 拼音 → 汉字，才成了输入法。

    pinyin_dict[0x4F60] = 'nǐ'      ← 它的方向
    chars['ni'] = ['你', '尼', ...]  ← 我们建的方向

两件必须做的事：

1. **去声调**。用户敲的是 "ni"，不是 "nǐ"。用 unicodedata 的 NFD 分解
   把声调符号拆出来滤掉。但 ü 要特殊处理 —— 它分解出来是 u + 分音符，
   直接滤就变成 "u" 了，而输入法里 ü 是用 "v" 打的（女 = "nv"）。

2. **音节切分**。"nihao" 要切成 "ni" + "hao" 才能查。音节表不另外找，
   直接从 pinyin_dict 的值里收集 —— 那本来就是全部合法音节。
"""

import bisect
import os
import threading
import unicodedata


# ü 系的字符要先单独换掉，再走 NFD。见文件头第 1 点。
_UMLAUT = {
    "ü": "v", "Ü": "v",     # ü Ü
    "ǖ": "v", "ǘ": "v",     # ǖ ǘ
    "ǚ": "v", "ǜ": "v",     # ǚ ǜ
}


def strip_tone(s):
    """'nǐ' -> 'ni'，'lǜ' -> 'lv'。

    先换 ü 系（否则 NFD 会把 ü 拆成 u + 分音符，滤完变 "u"），
    再用 NFD 把声调符号拆成独立字符滤掉。
    """
    if not s:
        return ""
    s = "".join(_UMLAUT.get(c, c) for c in s)
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if not unicodedata.combining(c))


class PinyinIME(object):
    """拼音 → 候选字/词。

    索引是**懒加载**的：构造的时候什么都不做，第一次要候选时才建，
    或者显式调 build()。因为建索引要解析 8 万条数据，在 RK3308 上
    不是一瞬间的事，不能拖慢程序启动。

    线程安全：build() 可以丢到后台线程跑，ready() 查状态。
    """

    def __init__(self, use_phrases=True, limit_chars=60):
        self.use_phrases = use_phrases
        # 一个音节最多留多少个候选字。'yi' 有 100 多个同音字，
        # 全留着既占内存又没用 —— 屏幕上就显示 4 个。
        self.limit_chars = limit_chars

        self.chars = {}          # 'ni' -> ['你', '尼', ...]（已按频率排序）
        self.phrases = {}        # 'nihao' -> ['你好', ...]
        self.syllables = set()   # 全部合法音节，切分要用
        self.max_syl = 6         # 最长音节（'zhuang' 是 6 个字母）

        self._ready = False
        self._lock = threading.Lock()
        self._freq = None        # 字频表，语义由 _freq_is_count 判定
        self._freq_is_count = False   # True = 值是频次，False = 是名次
        self.skipped_rare = 0    # 滤掉了多少个生僻字（诊断用）
        self._word_rank = {}     # 词 -> 词频名次（小 = 常用），见 _jieba_phrases

    # ---- 建索引 ----

    def build(self, freq=None):
        """建索引。可以在后台线程里调。

        freq 是 {字: 名次} 的字频表，用来给候选排序 —— 没有它的话，
        'ni' 的第一个候选会是 '尼' 而不是 '你'（按码点排的顺序），
        功能不变但很难用。
        """
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self._freq = freq or {}
            self._freq_is_count = self._detect_freq_kind()
            try:
                self._build_chars()
                if self.use_phrases:
                    self._build_phrases()
            except ImportError:
                # 没装 pypinyin。不崩，只是没有候选 —— 语音和英文照常。
                pass
            self._ready = True

    def _build_chars(self):
        from pypinyin import pinyin_dict as pd

        raw = {}
        syls = set()
        skipped_rare = 0
        for code, py in pd.pinyin_dict.items():
            # ⚠️ **先按 Unicode 区段滤掉生僻字**。这一步不能省。
            #
            # pypinyin 的字表含 41497 个字，其中 5775 个在**扩展 A 区**
            # （U+3400-4DBF，生僻字）。码点比对常用汉字区（U+4E00+）
            # **小**，所以只要按码点排或按"没频率"兜底排，生僻字就
            # 全体挤到前面去 —— 实测 'mao' 的候选前 22 个全是扩展 A 区
            # 的怪字，「冒」被顶到第 29 位，用户根本点不到。
            #
            # 日常打字用不到这些字（都是「㑵」「㒻」这种），直接不要。
            # 真要用生僻字的话语音也能兜底。
            if not (0x4E00 <= code <= 0x9FFF):
                skipped_rare += 1
                continue
            try:
                ch = chr(code)
            except (ValueError, OverflowError, TypeError):
                continue
            if not isinstance(py, str):
                continue
            # 一个字的多个读音用逗号分隔（成语里偶尔是别的写法，
            # 所以逗号和顿号都切）
            for one in py.replace("、", ",").split(","):
                key = strip_tone(one.strip())
                # 只要纯 ASCII 的音节。带数字声调（'ni2'）或别的东西
                # 一律不要 —— 用户敲的是字母。
                if not key or not key.isascii() or not key.isalpha():
                    continue
                syls.add(key)
                lst = raw.setdefault(key, [])
                if ch not in lst:
                    lst.append(ch)

        self.syllables = syls
        if syls:
            self.max_syl = max(len(s) for s in syls)
        self.skipped_rare = skipped_rare

        # 按常用度排序，再截断。
        f = self._freq
        for key, lst in raw.items():
            if f:
                # ⚠️ 用 rank() 而不是直接拿值排 —— 见 _ranker 的注释。
                # 一句话：有的表是"名次"（小=常用），有的是"频次"
                # （大=常用），方向相反，混用会把最常用的排到最后。
                lst.sort(key=lambda c: (self._rank(c), ord(c)))
            if self.limit_chars:
                lst = lst[:self.limit_chars]
            self.chars[key] = lst

    def _rank(self, ch):
        """返回排序用的"名次"，**越小越常用** —— 统一成一个方向。

        ⚠️ 这里踩过一次，值得记下来。

        两种字频数据的**语义是相反的**：

            pinyin_ime.common_freq()  →  {字: 名次}   名次小 = 常用
            jieba/dict.txt            →  {字: 频次}   频次大 = 常用

        原来的代码直接 `f.get(c, 99999)` 往升序里塞。对"名次"表是对的，
        一旦换成 jieba 的"频次"表，**最常用的字反而排到最后** ——
        实测 'mao' 的「毛」掉到第 51 位，比不排序还糟。

        所以这里统一：不管喂进来的是哪种，都换算成"名次"（小的在前）。
        怎么判断是哪种？看一个已知高频字的值就知道 —— 如果「的」的值
        很大（几万），那是频次；很小（几百以内），那是名次。
        """
        if not self._freq:
            return 99999
        raw = self._freq.get(ch)
        if raw is None:
            return 99999
        if self._freq_is_count:
            # 频次 → 名次：取负号让大的排前面
            return -raw
        return raw

    def _detect_freq_kind(self):
        """判断 _freq 是"名次"还是"频次"。

        找「的」—— 它是中文里最常见的字。名次表里它的值很小
        （个位数到几十），频次表里很大（几万以上）。

        ⚠️ 光看「的」不够。小表（比如测试里造的那几个字、或者用户
        自己塞的一张小表）里根本没有「的」，那就得看**整张表的量级**：

            名次表  值是 0,1,2,3...        最大也就表长
            频次表  值是 7753, 318825...   动辄上万

        所以兜底规则：表里的最大值 > 1000 就当频次表。
        这个界划得住 —— 一张有 1000 个字的**名次**表，最大名次才 999；
        而一张**频次**表只要有真实语料，常用字轻松上万。
        """
        if not self._freq:
            return False
        probe = self._freq.get("的")
        if probe is None:
            for c in "一是不了在有的":
                if c in self._freq:
                    probe = self._freq[c]
                    break
        if probe is not None:
            return probe > 1000
        # 「的」和那几个常用字都不在 —— 看整张表的量级
        try:
            return max(self._freq.values()) > 1000
        except (TypeError, ValueError):
            return False

    def _build_phrases(self):
        """整词候选：'nihao' -> ['你好', '泥壕', ...]。

        内存是大头：45471 条，只收**2~4 个字**的词（最有用），
        更长的丢掉 —— 板子只有 512MB，屏幕上也只显示 4 个候选。
        """
        from pypinyin import phrases_dict as ph

        out = {}
        for key, pys in ph.phrases_dict.items():
            try:
                word = "".join(key) if isinstance(key, tuple) else str(key)
            except Exception:
                continue
            if not (2 <= len(word) <= 4):
                continue
            parts = self._readings(pys)
            # 拼音组数必须和字数对得上，否则这个词的读音是错的，
            # 收进去会给出张冠李戴的候选
            if parts is None or len(parts) != len(word):
                continue
            out.setdefault("".join(parts), []).append(word)

        # pypinyin 的 phrases_dict 只有 4.5 万条，**缺常用词** ——
        # 比如「测试」整个不在里面（'ceshi' 只映射到「侧室/侧视」），
        # 用户打 ces 想看到「测试」就永远看不到。所以再补一层 jieba
        # 的高频词，按频次排在前面。
        try:
            extra, word_rank = self._jieba_phrases()
            # 词频名次（小 = 常用）。前缀猜词时按它排 ——
            # 'ces' 下面「测试」必须压在「侧身」前面。
            self._word_rank = word_rank
            for key, words in extra.items():
                if key in out:
                    # jieba 的词按频次排过，放前面
                    out[key] = words + [w for w in out[key] if w not in words]
                else:
                    out[key] = words
        except Exception:
            self._word_rank = {}     # 没装 jieba / 词典不在 —— 不影响主功能

        self.phrases = out
        # 前缀查找用的有序 key 表。
        #
        # 为什么要它：用户打 "ces" 时（还没敲完 "ceshi"），
        # segment("ces") 切不动（'ce' 是音节，'ces' 不是），
        # 走音节前缀自然找不到东西 —— 候选栏当场清空，
        # 和正常输入法"边打边猜"的体验差很远。
        # 拿整串去短语库里做前缀匹配就能出「测试」。
        # 39k 条 key 每次线性扫太慢，排序 + bisect 是 O(log n)。
        self._sorted_keys = sorted(out)

    @staticmethod
    def _read_words_file(path):
        """读预生成的 ime_words.txt：每行「拼音 词」，行序 = 词频名次。

        这是**快路径**。在板子上现算一遍要 20 多秒（解析 34 万行 +
        6000 次 lazy_pinyin），拼音表又是静态的，没理由每次开机重算。
        文件由 gen_words.py 生成，随项目一起部署。
        """
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except IOError:
            return None
        out = {}
        rank = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or " " not in line:
                continue
            key, word = line.split(" ", 1)
            out.setdefault(key, []).append(word)
            rank[word] = len(rank)      # 行序即名次
        if not out:
            return None
        return out, rank

    @staticmethod
    def _jieba_phrases(path=None, limit_words=6000):
        """从 jieba 的词频表里提取**常用词**，转成 {全拼: [词...]}。

        为什么要它：pypinyin 自带的 phrases_dict 覆盖不全，「测试」
        「今天」这种天天用的词都没有。jieba 的 dict.txt 有 34 万行、
        带真实语料频次，正好补这个洞。

        只取：
          · 2~4 个汉字（更长的屏幕上放不下，也不常打）
          · 纯汉字（带数字/字母的像 "mp3" 转不出拼音）
          · 频次最高的前 limit_words 个 —— 全转要好几秒，板子等不起

        拼音用 lazy_pinyin 现转，一个字是查表，很快。
        """
        import os
        # 快路径：预生成的词表就在本文件旁边
        here = os.path.dirname(os.path.abspath(__file__))
        fast = PinyinIME._read_words_file(os.path.join(here, "ime_words.txt"))
        if fast is not None:
            return fast

        p = path or JIEBA_DICT
        if not os.path.exists(p):
            return {}, {}
        # ⚠️ 逐行 split + 逐个字符判断在板子上要 11 秒（34 万行），
        # 改成一次读进内存用正则批出来，快 20 倍。
        import re
        text = open(p, encoding="utf-8", errors="replace").read()
        rows = [(int(f), w) for w, f in
                re.findall(r"^([\u4e00-\u9fff]{2,4}) (\d+)", text, re.M)]
        rows.sort(reverse=True)
        rows = rows[:limit_words]

        from pypinyin import lazy_pinyin
        out = {}
        for i, (_f, w) in enumerate(rows):
            try:
                key = "".join(lazy_pinyin(w))
            except Exception:
                continue
            out.setdefault(key, []).append(w)
        return out, {w: i for i, (_f, w) in enumerate(rows)}

    @staticmethod
    def _readings(pys):
        """把 phrases_dict 的值拍成每个字一个拼音串。

        pypinyin 里长这样（每个字给一组备选读音）：
            [['yī'], ['dìng'], ['bù'], ['shì']]
        但版本之间不保证，所以字符串形态也认：
            ['yī', 'dìng']
        两种都认，认不出返回 None。**宁可少收几条，也别收错**——
        读音错位的词会给出完全不相干的候选。
        """
        if not pys:
            return None
        out = []
        for grp in pys:
            if isinstance(grp, str):
                one = grp
            elif isinstance(grp, (list, tuple)) and grp:
                one = grp[0]
            else:
                return None
            if not isinstance(one, str):
                return None
            key = strip_tone(one.strip())
            if not key:
                return None
            out.append(key)
        return out

    def ready(self):
        return self._ready

    def build_async(self, freq=None, on_done=None):
        """后台建索引，别卡住界面。"""
        def work():
            self.build(freq)
            if on_done:
                try:
                    on_done(self._ready)
                except Exception:
                    pass
        t = threading.Thread(target=work)
        t.daemon = True
        t.start()
        return t

    # ---- 切分 ----

    def segment(self, s):
        """'nihao' -> ['ni', 'hao']；切不动返回 None。

        贪心最长匹配。音节表是从 4 万个字里收集的，覆盖全，
        所以贪心不会把 'nihao' 切成 'n' + 'ihao' 这种。
        切不动返回 None（用户可能敲了 'xq' 这种非法拼音）。
        """
        if not s:
            return []
        out = []
        i = 0
        n = len(s)
        while i < n:
            hit = None
            top = min(self.max_syl, n - i)
            for L in range(top, 0, -1):
                if s[i:i + L] in self.syllables:
                    hit = s[i:i + L]
                    break
            if hit is None:
                return None
            out.append(hit)
            i += len(hit)
        return out

    # ---- 候选 ----

    def candidates(self, py, limit=4):
        """给一个拼音串，返回候选列表（词优先，后面补单字）。

        还没建好索引就返回空 —— 界面照常显示，只是没候选。
        """
        py = (py or "").strip().lower()
        if not py or not self._ready:
            return []

        out = []
        seen = set()

        # 1) 整词。'nihao' 直接出 '你好'
        for w in self.phrases.get(py, ()):
            if w not in seen:
                seen.add(w)
                out.append(w)
            if len(out) >= limit:
                return out

        # 2) 按音节切，每个音节给它的常用字。
        #    'nihao' 切完 = ['ni','hao'] → 先给 'ni' 的字（用户先选第一个）
        syls = self.segment(py)
        if syls is None:
            # 切不动：用户多半还在打（'ce' 是音节，'ces' 不是）。
            #
            # 先拿**整串**去词库里做前缀匹配 —— 'ces' → 'ceshi' → 测试。
            # 这一步不能少：没有它，敲到一半候选栏就是空的，
            # 用户以为输入法坏了。（真正要看着候选决定怎么打的场景，
            # 恰恰就是"还没敲完"的时候。）
            if len(py) <= 2:
                # 两个字母以内还看不出要打什么词（'zh' 可能是这/中/只…），
                # 这时候给单字比给词靠谱。按字频排，别按字母序。
                for c in self._short_prefix_chars(py, limit):
                    if c not in seen:
                        seen.add(c)
                        out.append(c)
                if out:
                    return out[:limit]

            for w in self._prefix_words(py, limit):
                if w not in seen:
                    seen.add(w)
                    out.append(w)
                if len(out) >= limit:
                    return out[:limit]

            # 再退到音节前缀（'nih' → 'ni'/'nian'…）给单字，别让候选栏空着
            for k in self._prefix_keys(py, limit):
                for w in self.chars.get(k, ())[:1]:
                    if w not in seen:
                        seen.add(w)
                        out.append(w)
                if len(out) >= limit:
                    break
            return out[:limit]

        # 单音节：直接给这个音节的所有候选字
        if len(syls) == 1:
            for c in self.chars.get(syls[0], ()):
                if c not in seen:
                    seen.add(c)
                    out.append(c)
                if len(out) >= limit:
                    break
            return out

        # 多音节。三档，按"能一次吃掉的音节数"从多到少：
        #
        #   1) 整串词组 —— 上面第 1 步已经给了（'nihao' → 你好）
        #   2) 前缀词组 —— 'woaini' 整串没有词，但 'woai'(我爱)、'wo'(我)
        #      有，先给长的。没有这一档就只能一个字一个字打，
        #      两段话拼不成一句（用户报的就是这个）。
        #   3) 第一个音节的单字 —— 兜底，输入法最常见的行为。
        for k in range(len(syls) - 1, 0, -1):
            key = "".join(syls[:k])
            for w in self.phrases.get(key, ()):
                if w not in seen:
                    seen.add(w)
                    out.append(w)
                if len(out) >= limit:
                    return out[:limit]

        for c in self.chars.get(syls[0], ()):
            if c not in seen:
                seen.add(c)
                out.append(c)
            if len(out) >= limit:
                break
        return out[:limit]

    def _short_prefix_chars(self, py, limit):
        """按音节前缀取**单字**，按字频排。

        'zh' → 候选音节 zha/zhai/zhan/…/zhi/zhong/zhu，
        取每个音节最常用的那个字，再按字频排序 —— 出来就是
        中/这/只/正/主 这类真正常用的字。
        （按字母序取前 N 个只会得到 'zha...' 那一串冷僻词。）
        """
        pool = []
        for syl in self.syllables:
            if syl.startswith(py) and syl != py:
                cs = self.chars.get(syl)
                if cs:
                    pool.append(cs[0])
        pool = sorted(set(pool), key=self._rank)
        return pool[:limit]

    def _prefix_words(self, py, limit):
        """整串拼音前缀匹配词组：'ces' → 'ceshi' → 测试。

        用 bisect 找起点再往后扫，命中几条就停 —— 39k 条 key 全扫一遍
        在板子上要几十毫秒，每个字母都扫一次会明显卡。
        """
        keys = getattr(self, "_sorted_keys", None)
        if not keys or not py:
            return []
        # 只看 bisect 起点后面的**有限个** key。
        # 按字母序全扫的话，'zh' 会得到一堆 'zha...' 开头的冷僻词，
        # 而用户真正想要的「这(zhe)」「中(zhong)」排在几百条后面
        # —— 字母序 ≠ 常用度。所以扫一个有界窗口，再按字频重排。
        WINDOW = 240
        pool = []
        i = bisect.bisect_left(keys, py)
        end = min(len(keys), i + WINDOW)
        while i < end and keys[i].startswith(py):
            pool.extend(self.phrases.get(keys[i], ()))
            i += 1
        if not pool:
            return []
        # 先按**整词**词频（只有 jieba 补进来的词有），
        # 没有词频的再按首字常用度，最后一档短词优先
        wr = getattr(self, "_word_rank", {})
        pool.sort(key=lambda w: (wr.get(w, 10 ** 6), self._rank(w[0]), len(w)))
        out = []
        for w in pool:
            if w not in out:
                out.append(w)
            if len(out) >= limit:
                break
        return out

    def _prefix_keys(self, py, limit):
        """找以 py 开头的音节，按长度短的优先（更可能是用户想打的）。"""
        hits = [k for k in self.syllables
                if k.startswith(py) and k != py]
        hits.sort(key=lambda k: (len(k), k))
        return hits[:limit]

    def commit_word(self, py):
        """整个拼音串一次性转成最可能的词/字。

        给"不想选字，直接要个结果"的场景用（比如语音之外的手输兜底）。
        拿不到就返回 None。
        """
        c = self.candidates(py, limit=1)
        return c[0] if c else None


# ---------------------------------------------------------------- 字频表

# 单字候选的排序依据。没有它，'ni' 打出来第一个是 '尼' 不是 '你'。
# 这份是常用字表（按使用频率大致排的），前后顺序比精确名次重要。
COMMON = (
    "的一是不了在人有我他这个们中来上大为和国地到以说时要就出会可也你对生能而子那得于着下自之年过发后作里用道行所然家种事成方多经么去法学如都同现当没动面起看定天分还进好小部其些主样理心她本前开但因只从想实日军者意无力它与长把机十民第公此已工使情明性知全三又关点正业外将两高间由问很最重并物手应战向头文体政美相见被利什二等产或新己制身果加西斯月话合回特代内信表化老给世位次度门任常先海通教儿原东声提立及比员解水名真论处走义各入几口认条平系气题活尔更别打女变四神总何电数安少报才结反受目太量再感建务做接必场件计管期市直德资命山金指克许统区保至队形社便空决治展马科司五基眼书非则听白却界达光放强即像难且权思王象完设式色路记南品住告类求据程北边死张该交规万取拉格望觉术领共确传师观清今切院让识候带导争运笑飞风步改收根干造言联持组每济车亲极林服快办议往元英士证近失转夫令准布始怎呢存未远叫台单影具罗字爱击流备兵连调深商算质团集百需价花党华城石级整府离况亚请技际约示复病息究线似官火断精满支视消越器容照须九增研写称企八功吗包片史委乎查轻易早曾除农找装广显吧阿李标谈吃图念六引历首医局突专费号尽另周较注语仅考落青随选列武红响虽推势参希古众构房半节土投某案黑维革划敌致陈律足态护七兴派孩验责营星够章音跟志底站严巴例防族供效续施留讲型料终答紧黄绝奇察母京段依批群项故按河米围江织害斗双境客纪采举杀攻父苏密低朝友诉止细愿千值仍男钱破网热助倒育属坐帝限船脸职速刻乐否刚威毛状率甚独球般普怕弹校苦创假久错承印晚兰试股拿脑预谁益阳若哪微尼继送急血惊伤素药适波夜省初喜卫源食险待述陆习置居劳财环排福纳欢雷警获模充负云停木游龙树疑层冷洲冲射略范竟句室异激汉村哈策演简卡罪判担州静退既衣您宗积余痛检差富灵协角占配征修皮挥胜降阶审沉坚善妈刘读啊超免压银买皇养伊怀执副乱抗犯追帮宣佛岁航优怪香著田铁控税左右份穿衣亮晶庭户联"
)


def common_freq():
    """把 COMMON 转成 {字: 名次}。名次越小越常用。

    ⚠️ 这只是**兜底**。它只有 789 个字，覆盖不了多少 ——
    实测 'mao' 的常用字里只有「毛」在表里，「冒」「帽」「猫」都不在，
    结果它们只能按码点排，被生僻字挤到几十位去。

    **板子上应该用 `jieba_freq()`**，那张表有 11580 个字，
    而且频次是真实语料统计出来的。见那个函数的注释。
    """
    return {c: i for i, c in enumerate(COMMON)}


# jieba 词频表的位置。板子上装了 jieba，这个文件就在。
JIEBA_DICT = "/usr/local/lib/python3.7/dist-packages/jieba/dict.txt"


def jieba_freq(path=None):
    """从 jieba 的词频表里抠出单字的**频次**（越大越常用）。

    jieba 的 dict.txt 是 "词 频次 词性" 三列，34 万行 —— 里面既有
    词也有单字。我们只取**单字**行（第一列长度为 1），同一个字出现
    多次就取最大的那个频次。

    实测能提出 11580 个字，比内置的 common_freq()（789 个）强得多：

        的 318825   是 796991   毛 7753   冒 5231
        猫 1908     貌 870      帽 757    贸 225

    ⚠️ 这里返回的是**频次**（大 = 常用），而 common_freq() 返回的是
    **名次**（小 = 常用），语义相反。PinyinIME 会自己判断是哪种
    （见 _detect_freq_kind），调用方不用管。

    装不了 jieba 就返回 None，上层退回 common_freq()。
    """
    import os
    p = path or JIEBA_DICT
    if not os.path.exists(p):
        return None
    out = {}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and len(parts[0]) == 1 \
                        and parts[1].isdigit():
                    ch, v = parts[0], int(parts[1])
                    if ch not in out or v > out[ch]:
                        out[ch] = v
    except Exception:
        return None
    return out or None


def best_freq():
    """按优先级挑一份最好的字频表。

    优先 jieba（11580 字、真实语料），没有就退回内置的 789 字表。

    ⚠️ 先看预生成的 ime_chars.txt。现算 jieba_freq() 要把 34 万行的
    词典逐行 split 一遍，在板子上是 **11 秒** —— 每次启动都付这个代价
    没有意义，字频表又不会变。文件由 gen_words.py 生成。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cached = os.path.join(here, "ime_chars.txt")
    if os.path.exists(cached):
        try:
            out = {}
            with open(cached, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) == 2:
                        out[parts[0]] = int(parts[1])
            if out:
                return out
        except (IOError, ValueError):
            pass
    return jieba_freq() or common_freq()
