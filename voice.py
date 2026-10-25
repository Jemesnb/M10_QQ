#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音转文字 —— 录音 + 识别。

设计成**可插拔**的：`VoiceEngine` 是接口，`VoskEngine` 是本地方案。
以后想换讯飞（板子上已经装了 `df-xfyun-speech`）或者自建云端 Whisper，
只要再写一个子类，界面那边一行都不用改。

⚠️ 三条硬约束：

1. **识别在后台线程跑**。RK3308 解一秒钟的语音不是一瞬间的事，
   在主线程里做界面就死了。

2. **16kHz / 16bit 单声道是 Vosk 要的格式**。别的采样率得先转。

3. **录音的声道数要能配**。我的记录里写着"板子麦克风必须双声道，
   单声道会报错"，但 DFRobot 官方 `unihiker/Audio.py` 里写死单声道 ——
   **两者矛盾，只能实测**。所以做成配置项，打不开就自动降级。

识别结果**不直接发出去**，交给 TextBuffer 让用户改 —— 这是用户
明确要求的（"语音输入的文字要可以用键盘改"）。
"""

import array
import ctypes
import os
import sys
import threading
import time

try:
    import audioop
except ImportError:
    # audioop 在 Python 3.13 被移除了。板子上是 3.7，本来就有；
    # 这里兜一手是为了本地能跑测试（不兜的话 import voice 直接炸）。
    audioop = None


def left_channel(fragment):
    """16bit 立体声取左声道。等价 audioop.tomono(fragment, 2, 1.0, 0.0)。"""
    if audioop is not None:
        return audioop.tomono(fragment, 2, 1.0, 0.0)
    n = len(fragment) // 4
    out = bytearray(n * 2)
    out[0::2] = fragment[0:4 * n:4]
    out[1::2] = fragment[1:4 * n:4]
    return bytes(out)


def rms16(fragment):
    """16bit 小端样本的 RMS。等价 audioop.rms(fragment, 2)。"""
    if audioop is not None:
        return audioop.rms(fragment, 2)
    n = len(fragment) // 2
    if n <= 0:
        return 0
    a = array.array("h")
    a.frombytes(fragment[:n * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return int((sum(v * v for v in a) / float(n)) ** 0.5)


# ---- 板子专属：让 libvosk.so 能加载 ----
#
# ⚠️ 这段是给行空板（Debian 10 / GCC 8.3）打的补丁，别的机器上自动跳过。
#
# 板子的 libstdc++ 符号只到 GLIBCXX_3.4.25，而 vosk 的 libvosk.so 是用
# 新得多的工具链编的，要 44 个 cxx11 符号，板子上一个都没有：
#
#     undefined symbol: _ZNSt7__cxx1119basic_ostringstreamIcSt11char_traitsIcESaIcEEC1Ev
#
# Debian 10 是终版，apt 源里没更新的 libstdc++。所以从 Debian 11 的包里
# 抠了个 libstdc++.so.6.0.28（GCC 10.2）放在 vosk 目录里。
#
# ⚠️ 两点必须注意：
#
# 1. **必须 RTLD_GLOBAL 预加载**，光靠 LD_LIBRARY_PATH 不行 ——
#    libstdc++ 早就被 Python 自己或别的扩展加载过了，符号表里还是老版本。
#
# 2. **版本不能挑最新的**。Debian 12 的 6.0.30（GCC 12）要 glibc 2.33，
#    板子只有 2.28，加载直接失败。6.0.28 只要 GLIBC_2.18，可以。
#
# 加载失败不抛异常 —— 大不了语音用不了，**不能让程序起不来**。

_VOSK_LIBSTDCXX = ("/usr/local/lib/python3.7/dist-packages/vosk/"
                   "libstdc++.so.6.0.28")
_preloaded = False


def _preload_vosk_libs():
    """把新版 libstdc++ 预加载进全局符号表。只做一次，幂等。"""
    global _preloaded
    if _preloaded:
        return True
    if not os.path.exists(_VOSK_LIBSTDCXX):
        return False
    try:
        ctypes.CDLL(_VOSK_LIBSTDCXX, mode=ctypes.RTLD_GLOBAL)
        _preloaded = True
        return True
    except OSError:
        return False


class VoiceEngine(object):
    """语音识别引擎的接口。"""

    name = "none"

    def available(self):
        """现在能不能用（装了库吗、模型在吗）。"""
        return False

    def reason(self):
        """不能用的原因，给界面显示用。"""
        return ""

    def start(self):
        """开始录音。立刻返回，识别在后台跑。"""
        raise NotImplementedError

    def stop(self):
        """停止录音并返回识别结果（**阻塞**，要等解码完）。

        拿不到就返回 ""，绝不抛异常 —— 界面等的是文本，不是崩溃。
        """
        raise NotImplementedError

    def warm(self):
        """提前准备设备/模型（默认什么都不做）。"""

    def release(self):
        """释放设备（默认什么都不做）。"""

    def interim(self):
        """中间结果，用来实时显示"正在听到什么"。

        还没识别出东西就返回 ""。
        """
        return ""

    def busy(self):
        return False


class NullEngine(VoiceEngine):
    """啥也不做的引擎。

    没装 Vosk、或者模型没下的时候用它顶上，界面照常能用键盘打字，
    只有 🎤 按钮是灰的。**不能让缺个语音库就把整个程序拦住。**
    """

    name = "none"

    def __init__(self, why="没装语音引擎"):
        self._why = why

    def available(self):
        return False

    def reason(self):
        return self._why


class VoskEngine(VoiceEngine):
    """本地离线识别（Vosk）。

    模型（各约 40MB，从 alphacephei.com/vosk/models 下）：
        vosk-model-small-cn-0.22     中文，词错率约 23.5%
        vosk-model-small-en-us-0.15  英文，词错率约 9.9%

    ⚠️ 中文词错率 23.5% 意味着**每四五个字就错一个**。这不是缺陷，
    是设计前提 —— 语音负责出草稿，键盘负责改。
    """

    name = "vosk"

    def __init__(self, model_path, rate=16000, channels=1, device=None,
                 max_sec=30.0, live_decode=True,
                 on_partial=None, on_log=None):
        self.model_path = model_path
        self.rate = rate
        self.channels = channels
        self.device = device
        # 单次录音的硬上限（秒）。不做静音检测了，所以这是唯一的兜底：
        # 万一用户忘了按停止，到点也强制收尾，不会让麦克风开一整天。
        self.max_sec = max_sec
        self.live_decode = live_decode
        self.on_partial = on_partial or (lambda _t: None)
        self.on_log = on_log or (lambda _m: None)

        self._model = None
        self._rec = None
        self._stream = None
        self._pa = None
        self._thread = None
        self._stop_flag = False
        self._text = ""
        self._partial = ""
        # 这次录到的音频（单声道 16bit，云端识别要用）。
        # 30 秒上限 × 16k × 2 = 960KB，板子扛得住。
        self._pcm = bytearray()
        self._warm_started = False
        self._error = ""
        self._running = False
        # 采集是否还在进行（busy() 只看这个 —— 本地解码可能在
        # 采集结束后还在跑，不能拖住界面）
        self._capturing = False
        # live_decode 由构造函数参数决定（见 make_engine），这里不要再赋值，
        # 否则会把外面传进来的 False 又盖回 True。

    # ---- 可用性 ----

    def available(self):
        if not self._has_vosk():
            return False
        if not self.model_path or not os.path.isdir(self.model_path):
            return False
        return True

    def reason(self):
        if not self._has_vosk():
            return "没装 vosk（pip3 install vosk）"
        if not self.model_path or not os.path.isdir(self.model_path):
            return "模型不在：%s" % (self.model_path or "(没配)")
        return ""

    @staticmethod
    def _has_vosk():
        """装了 vosk 吗 —— 能**真的导入**才算。

        ⚠️ 只 catch ImportError 是不够的。板子上就是活例子：
        `import vosk` 会因为加载不了 libvosk.so 抛 **OSError**，
        不是 ImportError。只 catch ImportError 的话，这个异常会一路
        冒到 available() 外面，把界面打崩。

        所以这里先预加载新版 libstdc++（见文件头那段注释），
        再宽着接异常。
        """
        _preload_vosk_libs()
        try:
            import vosk                                        # noqa: F401
            return True
        except Exception:
            return False

    # ---- 模型 ----

    def load(self):
        """加载模型。**慢**（几十 MB 要读进内存），所以可以提前调。

        加载失败不抛异常，记在 _error 里，available() 会变 False。
        """
        if self._model is not None:
            return True
        _preload_vosk_libs()              # 必须在 import vosk 之前
        try:
            import vosk
            vosk.SetLogLevel(-1)          # 别让它往 stderr 刷一堆日志
            t0 = time.time()
            self._model = vosk.Model(self.model_path)
            self.on_log("语音模型已加载（%.1fs）" % (time.time() - t0))
            return True
        except Exception as exc:
            self._error = "%s" % (exc,)
            self.on_log("语音模型加载失败: %s" % (exc,))
            return False

    # ---- 录音 ----

    def warm(self):
        """提前把**模型和麦克风**准备好（不阻塞调用方）。

        ⚠️ 为什么必须提前：
          1. 加载模型真机实测 **4.6 秒**
          2. ALSA 打开录音流还要几百毫秒

        这两件事如果发生在 start() 之后（老写法就是在 _run 里做的），
        用户按下语音键、开口说话的头几秒**根本没在录** —— 真机实测
        "录 5 秒拿到 0 字节"。短句碰巧还能对上，长句开头被吃掉，
        云端识别出来自然一团糟。

        所以：进输入界面时就在后台热一下，按键时只负责"开始读"。
        """
        if self._warm_started or self._running:
            return
        self._warm_started = True
        t = threading.Thread(target=self._warm_job)
        t.daemon = True
        t.start()

    def _warm_job(self):
        try:
            if not self.load():
                # 加载失败就别把 _warm_started 留着 —— 否则以后再也不会重试
                self._warm_started = False
                return
            import vosk
            self._rec = vosk.KaldiRecognizer(self._model, self.rate)
            if self._stream is None:
                self._stream = self._open_stream()
            if self._stream is None:
                self._warm_started = False      # 允许下次重试
        except Exception as exc:
            self._warm_started = False
            self.on_log("预热失败: %r" % (exc,))

    def _decode_offline(self, pcm=None):
        """把这次攒下来的整段音频**离线**解一遍（本地兜底用）。

        云端成功时这段用不上；云端挂了就靠它。
        0.5 秒一块喂进去，一次性塞大块在 RK3308 上会更慢。
        """
        pcm = bytes(self._pcm) if pcm is None else pcm
        if not pcm or self._model is None:
            return
        try:
            import json
            import vosk
            rec = vosk.KaldiRecognizer(self._model, self.rate)
            step = 8000
            for i in range(0, len(pcm), step):
                rec.AcceptWaveform(pcm[i:i + step])
            res = json.loads(rec.FinalResult())
            text = (res.get("text") or "").strip()
            if text:
                text = text.replace(" ", "") if self._is_cjk(text) else text
                self._text = text
        except Exception as exc:
            self.on_log("本地离线解码失败: %r" % (exc,))

    def _drain(self):
        """倒掉录音流里积压的旧音频。

        流是一直开着的，上次的尾巴还堆在缓冲里 —— 不清掉的话，
        这次的开头会混进上一段。
        """
        if self._stream is None:
            return
        try:
            for _ in range(20):
                n = self._stream.get_read_available()
                if n <= 0:
                    break
                self._stream.read(min(n, 4000), exception_on_overflow=False)
        except Exception:
            pass

    def release(self):
        """关掉麦克风（离开输入界面时调）。

        录音流是**一直开着**的（为了按键立刻能录），不用时必须显式关，
        否则麦克风指示灯一直亮着，也不礼貌。
        """
        self._stop_flag = True
        self._close_stream()
        self._warm_started = False

    def start(self):
        """开始录音，后台线程里解码。"""
        if self._running:
            return False
        if not self.available():
            self._error = self.reason()
            return False

        # 预热还没做完就等一下（第一次进输入界面时可能还在加载模型）。
        # 宁可这里等，也不要录到残缺的音频。
        if self._stream is None or self._rec is None:
            self.warm()
            # 只等 2 秒。等太久界面会像死机（用户在按语音键的那一刻），
            # 而预热是进输入界面就开始的，正常早就好了。
            deadline = time.time() + 2.0
            while (self._stream is None or self._rec is None) \
                    and time.time() < deadline:
                time.sleep(0.05)
            if self._stream is None:
                self._warm_started = False       # 下次按再试一次
                self._error = "麦克风还在准备，再按一下"
                return False

        # 每次开录换一个干净的识别器，别把上一次的状态带进来
        try:
            import vosk
            self._rec = vosk.KaldiRecognizer(self._model, self.rate)
        except Exception:
            pass
        self._drain()

        self._stop_flag = False
        self._text = ""
        self._partial = ""
        # 这次录到的音频（单声道 16bit，云端识别要用）。
        # 30 秒上限 × 16k × 2 = 960KB，板子扛得住。
        self._pcm = bytearray()
        self._error = ""
        self._running = True
        self._capturing = True
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()
        return True

    def _open_stream(self):
        """开录音流。声道数不对就自动换一个再试。

        ⚠️ 单声道/双声道这件事**记录和官方代码是矛盾的**，只能试出来。
        这里先按配置开，失败就换，两边都记日志 —— 下次就知道该用哪个了。
        """
        import pyaudio

        self._pa = pyaudio.PyAudio()
        tried = []
        # 先试配置里的，再试另一个
        for ch in (self.channels, 2 if self.channels == 1 else 1):
            if ch in tried:
                continue
            tried.append(ch)
            try:
                kwargs = dict(format=pyaudio.paInt16, channels=ch,
                              rate=self.rate, input=True,
                              frames_per_buffer=4000)
                if self.device is not None:
                    kwargs["input_device_index"] = self.device
                s = self._pa.open(**kwargs)
                if ch != self.channels:
                    self.on_log("声道数 %d 打不开，改用 %d"
                                % (self.channels, ch))
                self.channels = ch
                return s
            except Exception as exc:
                self.on_log("开录音流失败(ch=%d): %s" % (ch, exc))
        return None

    def _run(self):
        """后台：录音 → 喂给识别器 → 攒结果。"""
        try:
            import json

            # 模型 / 录音流通常已经在 warm() 里准备好了。
            # 没有的话（比如没预热就调了 start）在这里补一次。
            if self._stream is None:
                if not self.load():
                    self._error = self._error or "模型加载失败"
                    return
                import vosk
                if self._rec is None:
                    self._rec = vosk.KaldiRecognizer(self._model, self.rate)
                self._stream = self._open_stream()
            if self._stream is None:
                self._error = "开不了录音设备"
                self.on_log("开不了录音设备（检查 config.REC_DEVICE）")
                return

            started = time.time()
            chunk_sec = 0.25
            blocks = max(1, int(self.rate * chunk_sec))

            # ⚠️ 这里**不做任何静音检测** —— 判断"说完了没有"交给云端。
            #
            # 之前那套 VAD（底噪门槛 + 静音 1.5 秒收尾）在真机上问题很多：
            # 底噪 255~300 和门槛贴在一起，噪声尖峰会被当成开口，
            # 然后 1.5 秒安静就收尾；用户按下键、停一下再说话，录音早停了。
            # 调这些阈值纯属给自己找事 —— 云端本来就会处理噪声和静音。
            #
            # 现在的规则极简：
            #   · 按一下开始录，一直录到**再按一下**
            #   · 只有 MAX_REC_SEC 兜底（防止忘记停，麦克风一直开着）
            while not self._stop_flag:
                try:
                    data = self._stream.read(blocks,
                                             exception_on_overflow=False)
                except Exception as exc:
                    self._error = "读录音出错: %s" % (exc,)
                    break
                if not data:
                    continue

                mono = left_channel(data) if self.channels > 1 else data
                self._pcm.extend(mono)

                if self.live_decode and self._rec is not None:
                    if self._rec.AcceptWaveform(mono):
                        res = json.loads(self._rec.Result())
                        part = (res.get("text") or "").strip()
                        if part:
                            part = part.replace(" ", "") \
                                if self._is_cjk(part) else part
                            self._text = (self._text + " " + part).strip()
                        self._partial = ""
                        self.on_partial("")
                    else:
                        res = json.loads(self._rec.PartialResult())
                        self._partial = (res.get("partial") or "").strip()
                        self.on_partial(self._partial)

                if self.max_sec and time.time() - started >= self.max_sec:
                    self.on_log("录音到 %.0f 秒上限，自动收尾" % self.max_sec)
                    break

            # 采集结束：先放掉"还在录"的标志，让界面立刻收尾
            self._capturing = False

            # 本地兜底：没实时解码的话，把整段离线解一遍（独立线程，
            # 别占着 _run，否则这一两秒内按语音键会被拒）
            if not self.live_decode:
                pcm = bytes(self._pcm)
                t = threading.Thread(target=self._decode_offline, args=(pcm,))
                t.daemon = True
                t.start()

        except Exception as exc:
            self._error = "%s" % (exc,)
            self.on_log("语音识别出错: %r" % (exc,))
        finally:
            # ⚠️ 这里**不关流**：留着下次按键立刻能录（开流要几百毫秒）。
            # 不用的时候由 release() 关。
            #
            # ⚠️ _capturing 必须在这里兜底清掉：上面有几条提前 return 的
            # 路径（开不了流、模型没加载），清不掉的话 busy() 永远是 True，
            # 界面会卡在"结束"状态，语音键再也按不动。
            self._capturing = False
            self._running = False

    @staticmethod
    def _is_cjk(s):
        """判断这串结果要不要**去掉空格**。

        两个模型的空格含义完全不同：
            中文：**每个字之间**都有空格 —— "你 好 世 界"
            英文：**每个词之间**有空格   —— "hello world"

        所以一律去空格会把英文粘成一坨，一律不去中文就是一格一个字。
        区分的依据是**空格切出来的块有多长**：中文的块基本是单字，
        英文的块是词。

        返回 True = 这是中文结果，该把空格去掉。
        """
        if not s:
            return False
        parts = [p for p in s.split(" ") if p]
        if not parts:
            return False
        han = sum(1 for c in s if "一" <= c <= "鿿")
        if han == 0:
            return False
        # 单字块占比高 = 中文的"一字一格"
        singles = sum(1 for p in parts if len(p) == 1)
        if len(parts) == 1:
            # 一整块没有空格：看汉字占多少
            return han * 2 >= len(s)
        return singles * 2 >= len(parts)

    def _close_stream(self):
        for obj, closer in ((self._stream, "stop_stream"),
                            (self._stream, "close")):
            if obj is None:
                continue
            try:
                getattr(obj, closer)()
            except Exception:
                pass
        self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        self._rec = None

    def request_stop(self):
        """让录音线程收尾，**立刻返回**（不 join）。

        ⚠️ 界面必须用这个，不能用 stop()。stop() 会 join 解码线程，
        在板子上可能要好几秒；那段时间主循环是卡死的，用户以为没点上
        就再点一次 —— 等 join 返回后那次点击才被处理，直接又开了一次录音。
        "闪回语音按钮 / 要点两三次才停" 就是这么来的。
        """
        self._stop_flag = True

    def text(self):
        """已经识别出来的文本（不阻塞、不 join）。"""
        return self._text

    def stop(self, timeout=15.0):
        """停止并等识别结果。**阻塞**，最多等 timeout 秒。"""
        self._stop_flag = True
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._running = False
        return self._text

    def wav(self):
        """把这次录到的音频包成 WAV（云端 API 要完整文件）。

        没录到东西就返回 b""，上层据此决定要不要发云端。
        注意这里已经是**单声道**了 —— 双声道录音时取的是左声道。
        """
        from cloud_asr import pcm_to_wav
        if not self._pcm:
            return b""
        # 整段原样打包，不做任何裁剪 —— 噪声/静音交给云端处理
        return pcm_to_wav(bytes(self._pcm), self.rate, 1)

    def interim(self):
        # 中间结果优先给 partial —— 用户想看到"正在听到什么"
        return self._partial or self._text

    def busy(self):
        # ⚠️ 只看**采集**是否在进行。本地离线解码可能还在后面跑，
        # 但界面该收尾就得收尾（云端上传不能等它）。
        return self._capturing

    def error(self):
        return self._error


# ---------------------------------------------------------------- 工厂

def make_engine(cfg, on_partial=None, on_log=None):
    """按配置造一个引擎。**永远返回一个可用的对象**。

    没装库、没下模型、配置写错 —— 一律退回 NullEngine。
    界面那边不用做任何判空，这是故意的：缺个语音库不该拦住整个程序。
    """
    engine_name = getattr(cfg, "ASR_ENGINE", "vosk")
    if engine_name == "none":
        return NullEngine("语音已关闭（config.ASR_ENGINE = none）")

    if engine_name == "vosk":
        # 中英文用不同模型，按用户选的输入语言切
        path = getattr(cfg, "ASR_MODEL_CN", "")
        eng = VoskEngine(
            path,
            rate=getattr(cfg, "REC_RATE", 16000),
            channels=getattr(cfg, "REC_CHANNELS", 1),
            device=getattr(cfg, "REC_DEVICE", None),
            max_sec=getattr(cfg, "MAX_REC_SEC", 30.0),
            # ⚠️ 云端当主力时**关掉边录边解**。
            # Vosk 在 RK3308 上解不动 16kHz 实时音频，和采集抢同一个线程
            # 会把 ALSA 缓冲挤爆、音频被静默丢弃（实测录 5 秒只剩 0.5 秒）。
            # 关掉之后只采集，录完再离线解一遍给兜底用。
            live_decode=(getattr(cfg, "ASR_PROVIDER", "none") == "none"),
            on_partial=on_partial, on_log=on_log)
        if not eng.available():
            return NullEngine(eng.reason())
        return eng

    return NullEngine("不认识的 ASR_ENGINE: %s" % engine_name)
