#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预生成 ime_words.txt —— 只跑一次，产物随项目走。

为什么不让板子每次启动都算：
    jieba 的 dict.txt 有 34 万行，解析 + 按词频排序 + 6000 次
    lazy_pinyin 转拼音，在 RK3308 上要 20 多秒。拼音表是**静态**的，
    没理由每次开机重算一遍。

输出格式（两列，空格分隔，**行序即词频名次**）：
    ceshi 测试
    ceshi 厕所
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pinyin_ime import JIEBA_DICT          # noqa: E402

OUT = os.path.join(HERE, "ime_words.txt")
LIMIT = 6000


def main():
    text = open(JIEBA_DICT, encoding="utf-8", errors="replace").read()
    rows = [(int(f), w) for w, f in
            re.findall(r"^([\u4e00-\u9fff]{2,4}) (\d+)", text, re.M)]
    print("词典里 2~4 字词：%d 条" % len(rows))
    rows.sort(reverse=True)
    rows = rows[:LIMIT]

    from pypinyin import lazy_pinyin
    seen = set()
    n = 0
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("# 拼音 词 —— 由 jieba dict.txt 前 %d 个高频词预生成，"
                 "行序即词频名次\n" % LIMIT)
        for _f, w in rows:
            if w in seen:
                continue
            seen.add(w)
            fh.write("%s %s\n" % ("".join(lazy_pinyin(w)), w))
            n += 1
    print("写出 %s：%d 行，%d 字节" % (OUT, n, os.path.getsize(OUT)))

    # 字频表也一起固化。不固化的话 best_freq() 每次启动都要把 34 万行的
    # jieba 词典重扫一遍，在板子上是 11 秒。
    from pinyin_ime import jieba_freq
    freq = jieba_freq() or {}
    cout = os.path.join(HERE, "ime_chars.txt")
    with open(cout, "w", encoding="utf-8") as fh:
        fh.write("# 字 频次 —— 由 jieba dict.txt 预生成，越大越常用\n")
        for ch, f in sorted(freq.items(), key=lambda kv: -kv[1]):
            fh.write("%s %d\n" % (ch, f))
    print("写出 %s：%d 行" % (cout, len(freq)))


if __name__ == "__main__":
    main()
