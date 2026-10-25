# -*- coding: utf-8 -*-
"""联调自检：从板子直连 NapCat 的 WebSocket，验证三件事。

    1. 板子（国内）能不能连上服务器（美国）—— 网络与延迟
    2. Token 认证对不对
    3. OneBot 协议通不通（收发 API 调用）

跑法：
    python3 selftest.py

三种结果对应三种问题：
    - 超时 / refused      → 网络或端口映射问题
    - 401 / 握手失败       → Token 不对
    - 有 data 返回         → 全通，可以跑 app.py 了
"""
import json
import os
import sys
import time

import websocket

# 从 config.py 读,别在这儿写死 —— 否则改配置要改两处
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

HOST = config.SERVER_HOST
PORT = config.SERVER_PORT
TOKEN = config.ACCESS_TOKEN

URL = "ws://%s:%d/" % (HOST, PORT)

try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass


def call(ws, action, params=None, echo="1"):
    """发一个 API 调用并等它的响应。"""
    ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo}))
    while True:
        raw = ws.recv()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            print("  [非 JSON] %s" % raw[:120])
            continue
        # 事件流里混着 API 响应，靠 echo 分辨
        if "echo" in payload and str(payload["echo"]) == echo:
            return payload
        print("  [事件] post_type=%s" % payload.get("post_type"))


def main():
    print("=" * 46)
    print("目标   %s" % URL)
    print("Token  %s..." % TOKEN[:6])
    print("=" * 46)

    header = {"Authorization": "Bearer " + TOKEN}
    t0 = time.time()
    try:
        ws = websocket.create_connection(URL, timeout=20, header=header)
    except Exception as exc:
        dt = (time.time() - t0) * 1000
        print("× 连接失败（%.0fms）: %r" % (dt, exc))
        if "401" in str(exc) or "403" in str(exc):
            print("  → Token 不对，或者 NapCat 没开鉴权")
        elif "timed out" in str(exc).lower():
            print("  → 超时。服务器 49532 没监听，或 NAT 映射没生效")
        else:
            print("  → 网络层就不通，先确认服务器上 ss -tlnp 有没有 49532")
        return 2

    dt = (time.time() - t0) * 1000
    print("√ 握手成功（%.0fms）" % dt)

    try:
        # --- 1. 我是谁 ---
        t0 = time.time()
        resp = call(ws, "get_login_info", echo="a")
        dt = (time.time() - t0) * 1000
        if resp is None:
            print("× get_login_info 没响应")
            return 3
        print("\n[get_login_info]  往返 %.0fms" % dt)
        print("  " + json.dumps(resp, ensure_ascii=False)[:300])
        data = resp.get("data") or {}
        if not data.get("user_id"):
            print("  ! 没拿到 QQ 号 —— 可能是没登录，或 retcode=%s"
                  % resp.get("retcode"))
        else:
            print("  → 登录账号 %s (%s)"
                  % (data.get("user_id"), data.get("nickname", "")))

        # --- 2. 运行状态 ---
        t0 = time.time()
        resp = call(ws, "get_status", echo="b")
        dt = (time.time() - t0) * 1000
        print("\n[get_status]  往返 %.0fms" % dt)
        print("  " + json.dumps(resp, ensure_ascii=False)[:300])

        # --- 3. 好友/群列表（顺带验证数据接口） ---
        resp = call(ws, "get_friend_list", echo="c")
        friends = (resp or {}).get("data") or []
        print("\n[get_friend_list]  好友 %d 个" % len(friends))
        for f in friends[:5]:
            print("    %s  %s" % (f.get("user_id"), f.get("nickname")))

        resp = call(ws, "get_group_list", echo="d")
        groups = (resp or {}).get("data") or []
        print("\n[get_group_list]  群 %d 个" % len(groups))
        for g in groups[:5]:
            print("    %s  %s" % (g.get("group_id"), g.get("group_name")))

        # --- 4. 静静听 10 秒事件 ---
        print("\n[事件流] 监听 10 秒（现在去群里发条消息试试）...")
        ws.settimeout(10)
        deadline = time.time() + 10
        got = 0
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                break
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if "echo" in payload:
                continue
            got += 1
            pt = payload.get("post_type")
            if pt == "message":
                print("  ← 消息 [%s] %s: %s"
                      % (payload.get("message_type"),
                         (payload.get("sender") or {}).get("nickname"),
                         payload.get("raw_message")))
            else:
                print("  ← %s" % pt)
        if got == 0:
            print("  （10 秒内没有事件。这正常 —— 没人发消息就没有事件）")

    finally:
        ws.close()

    print("\n" + "=" * 46)
    print("全通。可以跑 app.py 了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
