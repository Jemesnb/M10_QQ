#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端语音识别（Deepgram）。

为什么要云端：板子上的 Vosk small-cn 词错率约 23.5%（四五个字错一个），
用户实测"用一晚上直接红温"。云端的 nova-2 中文是一个量级的差距。

设计约束：
  · **只用标准库**（urllib + json）。板子 pip 装东西慢，装不了还得编。
  · 接口是「一句话，一次 POST」：板子先本地做静音切句，把整段 WAV 发过去。
    不玩流式 —— 流式要长连接、断线重连、增量合并，弱机上不值得。
  · **任何异常都往外抛**：调用方要靠它决定是否回落到本地 Vosk。
    这里绝不"吞掉错误返回空串"，那会让上层以为识别成功但没听清。
"""

import json
import struct
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://api.deepgram.com/v1/listen"


def pcm_to_wav(pcm, rate=16000, channels=1, width=2):
    """把裸 PCM 包成 WAV。云端 API 要完整的音频文件，不是裸流。"""
    if not pcm:
        return b""
    byte_rate = rate * channels * width
    block_align = channels * width
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                 byte_rate, block_align, width * 8)
    hdr += b"data" + struct.pack("<I", len(pcm))
    return hdr + pcm


class AsrError(Exception):
    """云端识别失败。上层捕获它然后回落到本地。"""


class DeepgramASR(object):
    """Deepgram 预录音识别。"""

    name = "deepgram"

    def __init__(self, api_key, model="nova-2", language="zh-CN",
                 timeout=10.0, endpoint=DEFAULT_ENDPOINT, smart=True):
        self.api_key = (api_key or "").strip()
        self.model = model
        self.language = language
        self.timeout = timeout
        self.endpoint = endpoint
        self.smart = smart

    # ---- 接口 ----

    def available(self):
        return bool(self.api_key)

    def reason(self):
        return "" if self.available() else "没配 Deepgram API key"

    def transcribe(self, wav, language=None):
        """发一段 WAV，返回识别文本（可能是空串 = 没听清）。

        网络错、鉴权错、额度用完 —— 一律抛 AsrError，由上层回落本地。
        """
        if not self.api_key:
            raise AsrError("没配 API key")
        if not wav:
            raise AsrError("没有音频")

        url = ("%s?model=%s&language=%s&smart_format=%s"
               % (self.endpoint, self.model, language or self.language,
                  "true" if self.smart else "false"))
        req = urllib.request.Request(
            url, data=wav, method="POST",
            headers={"Authorization": "Token " + self.api_key,
                     "Content-Type": "audio/wav"})

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # 401 key 错 / 402 额度用完 / 429 限流 —— 都要让上层知道
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise AsrError("HTTP %s %s" % (exc.code, detail))
        except Exception as exc:
            # 超时、DNS、断网（板子上很常见）
            raise AsrError("%r" % (exc,))

        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            channels = (data.get("results") or {}).get("channels") or []
            alts = (channels[0] or {}).get("alternatives") or []
            return ((alts[0] or {}).get("transcript") or "").strip()
        except (ValueError, IndexError, AttributeError, TypeError) as exc:
            raise AsrError("返回格式看不懂: %r" % (exc,))


def make_cloud(cfg):
    """按配置造云端引擎。没配就返回 None（上层据此只用本地）。"""
    provider = getattr(cfg, "ASR_PROVIDER", "none")
    if provider != "deepgram":
        return None
    eng = DeepgramASR(
        getattr(cfg, "DEEPGRAM_KEY", ""),
        model=getattr(cfg, "DEEPGRAM_MODEL", "nova-2"),
        language=getattr(cfg, "DEEPGRAM_LANG", "zh-CN"),
        timeout=getattr(cfg, "CLOUD_TIMEOUT", 10.0))
    return eng if eng.available() else None
