#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 接口层。所有阻塞的 SSH/SFTP 操作都写成同步 def，
由 FastAPI 自动丢进线程池，不会堵住事件循环。"""
import datetime as dt
import mimetypes
import os
import tempfile
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, device as dev, stats

app = FastAPI(title="KWP UI", docs_url="/api/docs", openapi_url="/api/openapi.json")

CACHE = stats.StatsCache()
DOWNLOAD_DIR = config.CACHE_DIR / "downloads"


@app.exception_handler(dev.DeviceError)
def _device_error(request, exc):
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc), "device_error": True},
    )


# ---------- 配置 ----------

@app.get("/api/config")
def get_config():
    return config.load()


@app.post("/api/config")
def set_config(payload: dict = Body(...)):
    cfg = config.save({"host": payload.get("host"), "port": payload.get("port")})
    return cfg


# ---------- 设备 ----------

@app.get("/api/device/ping")
def ping():
    k = dev.Kindle()
    started = time.time()
    try:
        k.run("true", timeout=6)
    except dev.DeviceError as exc:
        return {"online": False, "host": k.host, "detail": str(exc)}
    return {"online": True, "host": k.host, "latency_ms": int((time.time() - started) * 1000)}


@app.get("/api/device/status")
def status():
    return dev.Kindle().status()


@app.post("/api/device/discover")
def discover(payload: dict = Body(default={})):
    import asyncio
    subnet = (payload or {}).get("subnet") or None
    found = asyncio.run(dev.discover(subnet=subnet))
    return {"subnet": subnet or dev._subnet_of(config.load()["host"]), "found": found}


# ---------- 阅读统计 ----------

@app.get("/api/stats")
def get_stats(range: str = "all", refresh: int = 0, local: int = 0):
    k = dev.Kindle()
    # 用本次调用返回的状态，而不是全局属性：并发请求会互相覆盖 source，
    # 刚刷新完的响应可能被改写成「取自缓存」。
    state = CACHE.ensure(k, force=bool(refresh), local=bool(local))
    state.pop("path", None)
    if range not in stats.RANGE_LABELS:
        raise HTTPException(400, "不支持的时间范围：{}".format(range))
    data = CACHE.snapshot(range)
    data["host"] = k.host
    data.update(state)
    # db_error 以本次读为准：state 是读之前取的，里面若带旧值会盖掉刚查出来的错
    data.setdefault("db_error", None)
    return data


def _cache_state():
    return {
        "offline": CACHE.offline,
        "local": CACHE.local,
        "source": CACHE.source,
        "cached_at": int(CACHE.fetched_at) if CACHE.fetched_at else None,
        "cache_error": CACHE.last_error,
        "refreshing": CACHE.refreshing,
        "db_error": CACHE.db_error,
    }


@app.get("/api/stats/calendar")
def get_calendar(month: str = ""):
    today = dt.date.today()
    if month:
        try:
            year, mon = (int(x) for x in month.split("-", 1))
            dt.date(year, mon, 1)
        except (ValueError, TypeError):
            raise HTTPException(400, "月份格式应为 YYYY-MM")
    else:
        year, mon = today.year, today.month
    data = CACHE.calendar(year, mon)
    data["today"] = today.isoformat()
    data.update(_cache_state())
    return data


@app.get("/api/stats/day")
def get_day(date: str = ""):
    if date:
        try:
            target = dt.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    else:
        target = dt.date.today()
    data = CACHE.day(target)
    data.update(_cache_state())
    return data


@app.get("/api/stats/week")
def get_week(start: str = ""):
    if start:
        try:
            anchor = dt.date.fromisoformat(start)
        except ValueError:
            raise HTTPException(400, "起始日期格式应为 YYYY-MM-DD")
    else:
        anchor = dt.date.today()
    # 传任意一天都归到所在周的周一，前端不必自己算
    data = CACHE.week(stats.week_start_of(anchor))
    data["today"] = dt.date.today().isoformat()
    data.update(_cache_state())
    return data


# ---------- 洞察 / 阅读记录（只读缓存，不碰设备） ----------

@app.get("/api/stats/insights")
def get_insights(range: str = "all"):
    if range not in stats.RANGE_LABELS:
        raise HTTPException(400, "不支持的时间范围：{}".format(range))
    data = CACHE.insights(range)
    data.update(_cache_state())
    return data


def _session_args(period, book):
    if period not in stats.SESSION_PERIODS:
        raise HTTPException(400, "不支持的时间范围：{}".format(period))
    return period, book or None


@app.get("/api/stats/sessions")
def get_sessions(period: str = "7d", book: int = 0, limit: int = 500):
    p, b = _session_args(period, book)
    data = CACHE.sessions(p, b, max(1, min(limit, 1000)))
    data.update(_cache_state())
    return data


@app.get("/api/stats/sessions/export")
def export_sessions(period: str = "7d", book: int = 0, save: int = 0):
    """整份清单导出成 CSV，不受列表的条数上限影响。

    save=1 时直接写进 data/exports/ 并返回真实路径。窗口模式下走浏览器下载
    会弹系统保存框、前端拿不到结果，这个口子让「导出」有确定的回执。
    """
    p, b = _session_args(period, book)
    data = CACHE.sessions(p, b, limit=None)
    text = stats.sessions_csv(data)
    name = "kwp-sessions-{}.csv".format(dt.date.today().strftime("%Y%m%d"))
    if save:
        out_dir = config.DATA_DIR / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / name
        target.write_text(text, encoding="utf-8")
        return {
            "path": str(target),
            "name": name,
            "rows": len(data["sessions"]),
            "seconds": data["seconds"],
        }
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="{}"'.format(name),
                 "X-Kwp-Rows": str(len(data["sessions"])),
                 "Cache-Control": "no-store"},
    )


# ---------- 书库 ----------

@app.get("/api/files")
def list_files(path: str = "/mnt/us/documents"):
    k = dev.Kindle()
    target = dev.safe_path(path)
    parent = None
    if target != "/mnt/us":
        parent = target.rsplit("/", 1)[0] or "/mnt/us"
    return {"path": target, "parent": parent, "entries": k.listdir(target)}


@app.post("/api/files/upload")
def upload(path: str = Form(...), files: list[UploadFile] = File(...)):
    k = dev.Kindle()
    target_dir = dev.safe_path(path)
    saved, failed = [], []
    for item in files:
        name = os.path.basename(item.filename or "").strip()
        if not name:
            continue
        remote = "{}/{}".format(target_dir.rstrip("/"), name)
        try:
            item.file.seek(0)
            k.upload(item.file, remote)
            saved.append({"name": name, "size": item.size or 0})
        except Exception as exc:
            failed.append({"name": name, "error": str(exc)})
    return {"saved": saved, "failed": failed}


@app.get("/api/files/download")
def download(path: str):
    k = dev.Kindle()
    remote = dev.safe_path(path)
    if remote.rstrip("/") in dev.PROTECTED:
        raise HTTPException(400, "不能下载目录")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="kwp_", dir=str(DOWNLOAD_DIR))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        k.download(remote, tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    name = remote.rsplit("/", 1)[-1]
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return FileResponse(tmp, filename=name, media_type=media,
                        background=_cleanup(tmp))


def _cleanup(path):
    from starlette.background import BackgroundTask

    def _rm():
        try:
            os.remove(path)
        except OSError:
            pass

    return BackgroundTask(_rm)


@app.post("/api/files/delete")
def delete(payload: dict = Body(...)):
    k = dev.Kindle()
    paths = payload.get("paths") or []
    done, failed = [], []
    for p in paths:
        try:
            k.remove(p)
            done.append(p)
        except Exception as exc:
            failed.append({"path": p, "error": str(exc)})
    return {"deleted": done, "failed": failed}


@app.post("/api/files/mkdir")
def mkdir(payload: dict = Body(...)):
    k = dev.Kindle()
    parent = dev.safe_path(payload.get("path"))
    name = (payload.get("name") or "").strip().strip("/")
    if not name or "/" in name:
        raise HTTPException(400, "目录名不合法")
    k.mkdir("{}/{}".format(parent.rstrip("/"), name))
    return {"ok": True, "path": "{}/{}".format(parent.rstrip("/"), name)}


# ---------- 静态资源 ----------

class NoCacheStatic(StaticFiles):
    """本地小工具，静态文件不必缓存。

    StaticFiles 默认只发 ETag/Last-Modified，Chrome 会走启发式缓存直接复用旧文件，
    改完 app.js / style.css 刷新看不到变化，容易误判成代码没生效。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", NoCacheStatic(directory=str(config.WEB_DIR)), name="static")
