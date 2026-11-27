#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地配置：记住设备地址、端口与上次访问的目录。

设备 IP 是 DHCP 分配的，会变，所以只把它当「上次已知地址」用，
连不上时由 device.discover() 主动扫描局域网。
"""
import json
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
WEB_DIR = ROOT / "web"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULTS = {
    "host": "192.168.31.215",
    "port": 2222,
    "last_library_path": "/mnt/us/documents",
}

_cache = None


def load():
    global _cache
    if _cache is not None:
        return _cache
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    _cache = cfg
    return cfg


def save(patch):
    cfg = dict(load())
    for k, v in patch.items():
        if k in DEFAULTS and v is not None:
            cfg[k] = v
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    global _cache
    _cache = cfg
    return cfg
