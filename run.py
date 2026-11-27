#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动入口。

    python run.py            # 独立窗口（pywebview）
    python run.py --dev      # 开发模式，浏览器打开并自动重载
    python run.py --host 192.168.31.215
"""
import argparse
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

PORT = 8763


def pick_port(preferred=PORT):
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return preferred


def main():
    ap = argparse.ArgumentParser(description="KWP UI")
    ap.add_argument("--dev", action="store_true", help="浏览器模式 + 热重载")
    ap.add_argument("--host", help="覆盖 Kindle 地址")
    ap.add_argument("--port", type=int, default=0, help="覆盖本地监听端口")
    args = ap.parse_args()

    if args.host:
        from app import config
        config.save({"host": args.host})

    port = args.port or pick_port()

    if args.dev:
        webbrowser.open("http://127.0.0.1:{}".format(port))
        uvicorn.run("app.server:app", host="127.0.0.1", port=port, reload=True)
        return

    options = {"host": "127.0.0.1", "port": port, "log_level": "warning"}
    server = uvicorn.Server(uvicorn.Config("app.server:app", **options))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = "http://127.0.0.1:{}".format(port)
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.1)

    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        thread.join()
        return

    webview.create_window("KWP UI", url, width=1280, height=860, min_size=(980, 660))
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    sys.exit(main())
