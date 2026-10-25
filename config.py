# -*- coding: utf-8 -*-
"""连接配置。改这里就行，不用动代码。"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
LOG_PATH = os.path.join(DATA_DIR, "qqterm.log")

# ---------------------------------------------------------------- NapCat 连接
#
# NapCat 里要开的是 **正向 WebSocket 服务端**（它的配置项叫 websocketServers），
# 板子作为客户端主动连过去。方向别搞反：
#
#     板子 (WS 客户端)  ──主动连──▶  NapCat (WS 服务端)
#
# NAT 机上要分清容器端口和外网端口 —— 填的是**外网**那个：
#   容器 3001  ←─NAT 映射─→  外网 20056
#
# 当前这台服务器的映射（面板上配的）：
#   20056 -> 3001  OneBot WebSocket   ← 板子连这个
#   20057 -> ?     备用

SERVER_HOST = ""            # NapCat 服务器地址（填在 secrets_local.py 里）
SERVER_PORT = 20056
ACCESS_TOKEN = ""           # OneBot 的 access token

# 连不上时的重试间隔（秒）
RECONNECT_MIN = 3
RECONNECT_MAX = 60

# ---------------------------------------------------------------- 演示模式
#
# True = **不连服务器、不登 QQ**，直接喂一批假消息。
#
# 干嘛用的：调界面、排版、输入法的时候不用每次登真号。一个 QQ 号
# 登在两处会把另一处挤下线，而且真群里的消息你控制不了 —— 想测
# "超长群名""@ 一个陌生人""60 字不换行"，在真号上要碰运气，
# 在演示模式里想造什么造什么。
#
# 假消息在 democlient.py 里，想加就在 DEMO_MESSAGES 里加一条。
DEMO_MODE = True

# 演示模式下喂消息的速度倍率。2.0 = 快一倍，0.5 = 慢一倍。
# 想看清楚每一条就调小，想快速扫一遍排版就调大。
DEMO_SPEED = 1.0

# ---------------------------------------------------------------- 界面

# 一屏显示多少条消息
MSG_ROWS = 8
# 消息列表最多保留多少条（内存和翻页范围）
MAX_MESSAGES = 300
# 会话列表最多显示多少个。
#
# ⚠️ 这个数要**大于等于群数 + 好友数**，否则有一部分群/好友
# 压根不出现在列表里（看不等于没有，是根本没建出来）。
# 现在连的号有 34 个群，所以调到 120 留足余量 —— 反正每屏只画
# 4 个，多出来的只是列表里排着，不占屏幕。
MAX_SESSIONS = 120

# 快捷回复。240x320 的屏上打字太痛苦，预设几条常用的。
# 想发别的内容，改这里。
# （现在有 26 键和语音了，快捷回复仍然保留 —— 点一下最快）
QUICK_REPLIES = [
    "在",
    "好",
    "收到",
    "哈哈哈哈",
    "晚点说",
    "？",
]

# 是否只读。True = 只能看不能发（最省事，也避免误发）
READ_ONLY = False

# ---------------------------------------------------------------- 语音输入
#
# 本地跑，不上传。模型从 https://alphacephei.com/vosk/models 下，
# 解压到下面这两个路径。
#
#   vosk-model-small-cn-0.22      中文  42MB  词错率约 23.5%
#   vosk-model-small-en-us-0.15   英文  40MB  词错率约 9.9%
#
# ⚠️ 中文 23.5% 意味着**每四五个字错一个**。这是设计前提不是缺陷：
#    语音负责出草稿，键盘负责改（识别结果会落进输入框，不会直接发出去）。

ASR_ENGINE = "vosk"          # "vosk" = 本地；"none" = 关掉语音只用键盘

# 模型目录。留空或者目录不存在，语音按钮会自动变灰、退回纯键盘，
# **不会**影响打字和收发消息。
ASR_MODEL_CN = "/root/M10_QQ/models/vosk-model-small-cn-0.22"
ASR_MODEL_EN = "/root/M10_QQ/models/vosk-model-small-en-us-0.15"

# 录音参数。16kHz 单声道 16bit 是 Vosk 要的格式，别乱改。
REC_RATE = 16000

# ⚠️ 声道数要实测。我的记录里写着"板子麦克风必须双声道，单声道会报错"，
#    但 DFRobot 官方 unihiker/Audio.py 里写死的是单声道 —— 两者矛盾。
#    代码会自动两种都试一遍并打日志，这个值只是"先试哪个"。
REC_CHANNELS = 1

# 录音设备号。None = 用系统默认。板子上默认是 [18] default，
# 声卡本身是 [2] rockchip,rk3308-acodec。
REC_DEVICE = None

MAX_REC_SEC = 30.0

# ---- 语音识别：云端主力 + 本地兜底 ----
#
# 为什么云端当主力：板子上的 Vosk small-cn 词错率约 23.5%（四五个字错一个），
# 实测没法用。云端 nova-2 中文好一个量级；本地只在云端不可用时顶上。
#
# ASR_PROVIDER:
#   "deepgram"  云端优先，失败回落本地 Vosk
#   "none"      只用本地（不联网、不上传音频）
ASR_PROVIDER = "deepgram"
DEEPGRAM_KEY = ""          # Deepgram API key
DEEPGRAM_MODEL = "nova-3"
DEEPGRAM_LANG = "zh-CN"       # 说英文多就改 en-US
# 云端超时（秒）。实测板子上传带宽有限：3 秒音频 2.4 秒往返，
# 10 秒音频 8.5 秒往返 —— 大约每 1 秒音频要 0.8 秒。30 秒上限的话
# 最坏 25 秒左右，给 30 留点余量。
CLOUD_TIMEOUT = 30.0

# ---------------------------------------------------------------- 私密配置
#
# 服务器地址、OneBot token、Deepgram key 这些**不能进仓库**。
# 把它们写在同目录的 secrets_local.py（已在 .gitignore 里），
# 这里在最后统一覆盖上来 —— 代码不用改，仓库也是干净的。
# 没有这个文件也能跑：只是连不上服务器、云端语音不可用。
try:
    from secrets_local import *          # noqa: F401,F403
except ImportError:
    pass
