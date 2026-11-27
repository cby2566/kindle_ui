#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阅读统计：把 KOReader 的 statistics.sqlite3 读成前端可直接画的结构。

聚合口径刻意对齐 KOReader 的 statistics.koplugin（源码已核对）：
  * 日历每格的小时直方图 —— getReadingRatioPerHourByDay()
  * 跨天书签合并 —— calendarview.lua CalendarWeek:addDay() 的 span_days 算法
  * 每天最多 N 个书位、超出显示 +N —— calendar_nb_book_spans（默认 3）
  * 单日时间轴 —— getReadingDurationBySecond()，30 秒内的间隙视为连续阅读
  * 页码缩放 —— page_stat 是视图，按当前 book.pages 重排 page_stat_data

库结构（DB_SCHEMA_VERSION 20221111）：
    book(id, title, authors, notes, last_open, highlights, pages, series,
         language, md5, total_read_time, total_read_pages)
    page_stat_data(id_book, page, start_time, duration, total_pages)
时间戳是 Unix 秒，由设备按本地时区记录。
"""
import csv
import contextlib
import datetime as dt
import io
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import config
from .device import DeviceError

# KOReader 默认值
DEFAULT_MIN_READ_SEC = 5
DEFAULT_MAX_READ_SEC = 120
LANES = 3                  # calendar_nb_book_spans
WEEK_START = 1             # 1 = 周一（对应 calendar_start_day_of_week = 2）

RANGE_LABELS = {
    "today": "今天",
    "week": "本周",
    "month": "本月",
    "year": "今年",
    "all": "全部",
}

REMOTE_DB = "/mnt/us/koreader/settings/statistics.sqlite3"


class _RWLock:
    """读多写一：读可以并行，换缓存文件时独占。

    为什么需要它：Windows 上只要还有别的线程开着这个文件，os.replace 就被拒绝
    （WinError 5 / 32），靠重试追不上持续不断的读请求。让读者在查询期间持共享锁、
    替换时持独占锁，替换就一定落在没人的窗口里。写者优先（waiting_writers），
    免得刷新被源源不断的读请求饿死。
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextlib.contextmanager
    def read(self):
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if not self._readers:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def write(self):
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


class StatsCache:
    """本地统计库缓存。设备不可达时退化为只读缓存，保证离线也能看历史。

    缓存文件是所有统计接口共用的，所以改写它必须原子：先下载到 .part，
    校验通过再 os.replace 换上去。读者要么看到完整的旧库、要么看到完整的
    新库，永远不会撞见写了一半的文件（那是之前 500 和「全 0」的根因）。
    """

    # 拉库是全进程唯一的写操作，串行化掉，避免并发刷新互相踩。
    # 类属性：同一个进程里不管 new 几个 StatsCache，都共用这把锁。
    _fetch_lock = threading.Lock()
    _rw = _RWLock()              # 读查询共享、替换缓存独占
    _replace_retries = 25        # 别的进程占着缓存时兜底重试
    _replace_wait = 0.04

    def __init__(self):
        self.db_path = config.CACHE_DIR / "statistics.sqlite3"
        self.fetched_at = self._file_mtime()
        self.offline = False
        self.local = False          # 本地模式：只读缓存，完全不碰设备
        self.source = "cache"       # device = 本次真的连上设备拿了新库
        self.last_error = None
        self.db_error = None        # 缓存库本身读不出来（本地文件损坏）

    @property
    def part_path(self):
        return Path(str(self.db_path) + ".part")

    @property
    def refreshing(self):
        """此刻是否有请求真的在拉库。

        直接问那把锁，不再另外维护标志位：手工置位总要在拉库结束那一刻清掉，
        等响应组装时早就变回 False 了（所以 /api/stats 里这个字段一直是假话）。
        锁自己就能回答「现在有没有人占着」，也不怕并发请求互相改乱。
        """
        return self._fetch_lock.locked()

    def _file_mtime(self):
        try:
            return self.db_path.stat().st_mtime
        except OSError:
            return 0.0

    def _clear_sidecars(self):
        """清掉 -wal / -shm / -journal。

        只下载主库，若旧的 WAL 还留着，SQLite 会把过期增量回放到新库上，
        轻则读数错乱、重则报 malformed。设备侧 KOReader 每次 insertDB 后
        都会关连接触发 checkpoint，实测 -wal 恒为 0 字节，取主库就够。
        Windows 上这些文件可能正被并发读请求开着，删不掉时重试几次就放过：
        主库没换之前留着无害，换之后 SQLite 会按新库重建。
        """
        for suffix in ("-wal", "-shm", "-journal"):
            target = Path(str(self.db_path) + suffix)
            for _ in range(3):
                try:
                    target.unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.02)

    def has_cache(self):
        return self.db_path.exists() and self.db_path.stat().st_size > 0

    def _is_fresh(self, max_age):
        return self.has_cache() and (time.time() - self.fetched_at) < max_age

    def _validate(self, path):
        """下载完先验一遍，再决定要不要换上去。

        网络半途断掉时 paramiko 会抛异常，但断在协议边缘也可能落一个长度
        不为 0 的残文件。这里查三样：非空、quick_check 通过、有 page_stat_data 表。
        """
        path = Path(path)
        if path.stat().st_size <= 0:
            raise DeviceError("统计库下载不完整：文件是空的")
        con = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            has_table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'page_stat_data'"
            ).fetchone()
        finally:
            con.close()
        if not row or row[0] != "ok":
            raise DeviceError("统计库下载不完整：{}".format(row[0] if row else "校验无返回"))
        if not has_table:
            raise DeviceError("统计库下载不完整：缺少 page_stat_data 表")

    def _replace(self, part):
        """把校验过的副本换到 db_path。"""
        error = None
        for _ in range(self._replace_retries):
            try:
                os.replace(part, self.db_path)
                return
            except OSError as exc:          # Windows：旧库正被读请求打开
                error = exc
                time.sleep(self._replace_wait)
        raise DeviceError("统计库替换失败（本地缓存文件被占用）：{}".format(error))

    def _fetch(self, kindle):
        """下载 → 校验 → 原子替换。任何一步失败都保留原缓存不动。

        返回本次结果 (source, offline, cache_error)，供 ensure() 组装响应——
        直接从共享属性读会被并发请求互相覆盖。
        """
        timeout = 4 if self.has_cache() else 8
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        part = self.part_path
        try:
            kindle.download(REMOTE_DB, part, timeout=timeout)
            self._validate(part)
        except Exception as exc:
            try:
                part.unlink()
            except OSError:
                pass
            if not self.has_cache():
                raise DeviceError(
                    "设备未连接，且本地还没有统计缓存。请先连上设备读取一次，"
                    "之后即使断开也能查看历史数据。"
                ) from exc
            self.offline = True
            self.source = "cache"
            self.last_error = str(exc) or exc.__class__.__name__
            self.fetched_at = self._file_mtime()
            return "cache", True, self.last_error
        with self._rw.write():
            self._clear_sidecars()
            self._replace(part)
            self._clear_sidecars()
        self.fetched_at = time.time()
        self.offline = False
        self.source = "device"
        self.last_error = None
        self.db_error = None
        return "device", False, None

    def _result(self, source, offline, cache_error, local=None):
        """本次调用的结果快照（不读会被并发请求改写的 source/offline/local）。

        这里不含 db_error：它是「读缓存库」的结论，归读接口自己带。
        ensure() 先返回、snapshot() 后读，若在这里读上一次留下的值，
        会盖掉本次读刚查出来的错误。
        """
        return {
            "path": str(self.db_path),
            "source": source,
            "offline": offline,
            "local": self.local if local is None else local,
            "refreshing": self.refreshing,
            "cached_at": int(self.fetched_at) if self.fetched_at else None,
            "cache_error": cache_error,
        }

    def ensure(self, kindle, max_age=90, force=False, local=False):
        """拉取统计库，返回**本次调用**的结果（含 path / source / offline 等）。

        设备连不上但本地有缓存时不抛异常，而是标记 offline=True 继续用缓存，
        历史数据照样能看。

        local=True 时彻底跳过设备：只读本地缓存，不再探活、不再下载。
        适用于「手头没有设备、只想翻历史」的场景，省掉每次都干等超时。
        """
        self.local = bool(local)
        if local:
            if not self.has_cache():
                raise DeviceError(
                    "本地模式需要至少连过一次设备：本地还没有统计库缓存。"
                    "请先关闭本地模式、连上设备读取一次。"
                )
            self.offline = False
            self.source = "local"
            self.last_error = None
            self.fetched_at = self._file_mtime()
            return self._result("local", False, None, local=True)

        if self._is_fresh(max_age) and not force:
            self.offline = False
            self.source = "cache"
            self.last_error = None
            return self._result("cache", False, None)

        before = self._file_mtime()
        with self._fetch_lock:
            # 等锁期间别人可能已经拉完并换过文件，别重复下一遍
            if (self.has_cache() and self._file_mtime() != before) or \
                    (not force and self._is_fresh(max_age)):
                self.offline = False
                self.source = "cache"
                self.last_error = None
                self.fetched_at = self._file_mtime()
                outcome = ("cache", False, None)
            else:
                outcome = self._fetch(kindle)
        # 出了锁再组装响应：refreshing 问的是「此刻还有没有别人在拉库」，
        # 停在锁里取会把自己也算进去。
        source, offline, error = outcome
        return self._result(source, offline, error)

    def _connect(self):
        return sqlite3.connect("file:{}?mode=ro".format(self.db_path.as_posix()), uri=True)

    def _read(self, builder, empty):
        """读缓存库。本地文件坏掉时不再抛 500，退回空结构并记下原因。

        有原子替换兜底后这条路径基本不会命中；留着是为了万一本机磁盘层面
        坏掉时，界面能说清「缓存库坏了」，而不是整页 500。
        """
        if not self.db_path.exists():
            return empty()
        try:
            with self._rw.read():
                con = self._connect()
                try:
                    data = builder(con)
                finally:
                    con.close()
        except sqlite3.Error as exc:
            self.db_error = str(exc) or exc.__class__.__name__
            data = empty()
            data["db_error"] = self.db_error
            return data
        self.db_error = None
        return data

    def snapshot(self, range_key="all"):
        data = self._read(lambda con: build_snapshot(con, range_key),
                          lambda: empty_snapshot(range_key))
        data["fetched_at"] = int(self.fetched_at)
        data["age_sec"] = int(time.time() - self.fetched_at)
        return data

    def calendar(self, year, month):
        return self._read(lambda con: build_calendar(con, year, month),
                          lambda: empty_calendar(year, month))

    def day(self, date):
        return self._read(lambda con: build_day(con, date),
                          lambda: empty_day(date))

    def week(self, start):
        return self._read(lambda con: build_week(con, start),
                          lambda: empty_week(start))

    def insights(self, range_key="all"):
        return self._read(lambda con: build_insights(con, range_key),
                          lambda: empty_insights(range_key))

    def sessions(self, period="7d", book=None, limit=500):
        return self._read(lambda con: build_sessions(con, period, book, limit),
                          lambda: empty_sessions(period))


# ---------- 通用 ----------

def _cutoff(range_key, now=None):
    now = now or dt.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "today":
        return today
    if range_key == "week":
        return today - dt.timedelta(days=today.weekday())
    if range_key == "month":
        return today.replace(day=1)
    if range_key == "year":
        return today.replace(month=1, day=1)
    return None


def _page_rows(con):
    return con.execute(
        "SELECT id_book, page, start_time, duration FROM page_stat_data "
        "WHERE duration > 0 AND start_time > 0 ORDER BY start_time"
    ).fetchall()


def _books_meta(con):
    meta = {}
    for bid, title, authors, pages, last_open, total_read_time in con.execute(
            "SELECT id, title, authors, pages, last_open, total_read_time FROM book"):
        meta[bid] = {
            "title": (title or "未命名").strip() or "未命名",
            "authors": (authors or "").strip(),
            "pages": pages or None,
            "last_open": last_open or 0,
            "total_read_time": total_read_time or 0,
        }
    return meta


# ---------- 概览（按时间范围） ----------

def build_snapshot(con, range_key="all"):
    rows = _page_rows(con)
    meta = _books_meta(con)
    cutoff = _cutoff(range_key)
    cutoff_ts = cutoff.timestamp() if cutoff else None

    scoped = [r for r in rows if cutoff_ts is None or r[2] >= cutoff_ts]

    # 连续阅读天数要看全量数据，不能被范围影响
    all_days = {}
    for _, _, ts, duration in rows:
        d = dt.datetime.fromtimestamp(ts).date()
        all_days[d] = all_days.get(d, 0) + duration

    daily, daily_pages, hourly, weekday = {}, {}, [0] * 24, [0] * 7
    book_acc = {}
    total = 0
    for bid, page, ts, duration in scoped:
        moment = dt.datetime.fromtimestamp(ts)
        d = moment.date()
        daily[d] = daily.get(d, 0) + duration
        daily_pages[d] = daily_pages.get(d, 0) + 1
        hourly[moment.hour] += duration
        weekday[moment.weekday()] += duration
        total += duration
        acc = book_acc.setdefault(bid, {"seconds": 0, "pages": set(), "last": 0})
        acc["seconds"] += duration
        acc["pages"].add(page)
        acc["last"] = max(acc["last"], ts)

    today = dt.date.today()
    # 图表用的连续日序列，最多回看 366 天：[日期, 分钟, 翻页数]
    series = []
    for i in range(366 - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        if cutoff and d < cutoff.date():
            continue
        series.append([d.isoformat(), round(daily.get(d, 0) / 60, 1), daily_pages.get(d, 0)])

    active_days = len(daily)
    longest, run, prev = 0, 0, None
    for d in sorted(all_days):
        run = run + 1 if prev and (d - prev).days == 1 else 1
        longest = max(longest, run)
        prev = d

    streak = 0
    cursor = today if today in all_days else today - dt.timedelta(days=1)
    while cursor in all_days:
        streak += 1
        cursor -= dt.timedelta(days=1)

    books = []
    for bid, acc in book_acc.items():
        m = meta.get(bid, {})
        total_pages = m.get("pages")
        progress = None
        if total_pages:
            progress = min(100.0, round(len(acc["pages"]) * 100.0 / total_pages, 1))
        books.append({
            "id": bid,
            "title": m.get("title", "未命名"),
            "authors": m.get("authors", ""),
            "seconds": int(acc["seconds"]),
            "pages_read": len(acc["pages"]),
            "pages_total": total_pages,
            "progress": progress,
            "last_read": int(acc["last"]),
        })
    books.sort(key=lambda b: b["seconds"], reverse=True)

    speed = round(len(scoped) / (total / 3600), 1) if total else 0

    return {
        "range": range_key,
        "range_label": RANGE_LABELS.get(range_key, range_key),
        "range_start": cutoff.date().isoformat() if cutoff else None,
        "overview": {
            "total_seconds": int(total),
            "total_books": len(books),
            "page_records": len(scoped),
            "active_days": active_days,
            "current_streak": streak,
            "longest_streak": longest,
            "avg_minutes": round(total / 60 / active_days, 1) if active_days else 0,
            "speed": speed,
            "last_read": max((b["last_read"] for b in books), default=0) or None,
        },
        "daily": series,
        "hourly": [round(v / 60, 1) for v in hourly],
        "weekday": [round(v / 60, 1) for v in weekday],
        "books": books[:40],
        "all_time_seconds": int(sum(all_days.values())),
    }


# ---------- 日历视图 ----------

def build_calendar(con, year, month):
    """月历数据：每天 24 小时直方图 + 跨天书签 + 书位分配。"""
    first = dt.date(year, month, 1)
    last = (dt.date(year, month + 1, 1) - dt.timedelta(days=1)) if month < 12 \
        else dt.date(year, 12, 31)
    start_ts = int(dt.datetime.combine(first, dt.time.min).timestamp())
    end_ts = int(dt.datetime.combine(last, dt.time.max).timestamp())

    rows = con.execute(
        "SELECT id_book, page, start_time, duration FROM page_stat_data "
        "WHERE duration > 0 AND start_time BETWEEN ? AND ? ORDER BY start_time",
        (start_ts, end_ts)).fetchall()
    meta = _books_meta(con)

    days = {}
    for bid, page, ts, duration in rows:
        moment = dt.datetime.fromtimestamp(ts)
        d = moment.date()
        cell = days.setdefault(d, {"seconds": 0, "hours": [0] * 24, "books": {}})
        cell["seconds"] += duration
        cell["hours"][moment.hour] += duration
        b = cell["books"].setdefault(bid, {"seconds": 0, "pages": set()})
        b["seconds"] += duration
        b["pages"].add(page)

    index_days = []
    cursor = first
    while cursor <= last:
        index_days.append(cursor)
        cursor += dt.timedelta(days=1)

    # ---- 书位分配与跨天合并（移植 CalendarWeek:addDay）----
    weeks = []       # 每周一个数组，元素为 7 天的 {date, slots}
    lane_days = []   # 与 index_days 对齐的 slots
    week_of_day = {}

    week_cursor = None
    for i, d in enumerate(index_days):
        if week_cursor is None or d.weekday() == WEEK_START - 1:
            week_cursor = []
            weeks.append(week_cursor)
        week_of_day[i] = len(weeks) - 1

        cell = days.get(d)
        slots = [None] * LANES
        if cell:
            ordered = sorted(cell["books"].items(), key=lambda kv: kv[1]["seconds"], reverse=True)
            for lane, (bid, acc) in enumerate(ordered[:LANES]):
                slots[lane] = {
                    "id": bid,
                    "title": meta.get(bid, {}).get("title", "未命名"),
                    "seconds": int(acc["seconds"]),
                    "start_day": i,
                    "span_days": 1,
                }
        week_cursor.append({"date": d, "slots": slots})
        lane_days.append(slots)

    # 同周内向前合并：同一本书连续两天落在同一书位，就连成一条
    pos = {d.isoformat(): i for i, d in enumerate(index_days)}
    for week in weeks:
        for d_idx in range(1, len(week)):
            slots = week[d_idx]["slots"]
            prev_slots = week[d_idx - 1]["slots"]
            gi = pos[week[d_idx]["date"].isoformat()]
            for pn in range(LANES):
                prev_book = prev_slots[pn]
                if not prev_book:
                    continue
                for tn in range(LANES):
                    this_book = slots[tn]
                    if this_book and this_book["id"] == prev_book["id"]:
                        this_book["start_day"] = prev_book["start_day"]
                        this_book["span_days"] = prev_book["span_days"] + 1
                        for bk in range(prev_book["span_days"]):
                            back = lane_days[gi - bk - 1]
                            if back[pn]:
                                back[pn]["span_days"] = this_book["span_days"]
                        if tn != pn:
                            slots[tn], slots[pn] = slots[pn], slots[tn]
                        break

    # 只在 span 起始日输出线
    spans = []
    for i, slots in enumerate(lane_days):
        for lane, b in enumerate(slots):
            if b and b["start_day"] == i:
                spans.append({
                    "id": b["id"],
                    "title": b["title"],
                    "start_day": i,
                    "days": b["span_days"],
                    "lane": lane,
                    "week": week_of_day[i],
                    "week_col": index_days[i].weekday() - (WEEK_START - 1),
                })

    payload_days = []
    max_hour = 1
    max_day = 1
    for i, d in enumerate(index_days):
        cell = days.get(d)
        hours = cell["hours"] if cell else [0] * 24
        max_hour = max(max_hour, max(hours))
        seconds = cell["seconds"] if cell else 0
        max_day = max(max_day, seconds)
        n_books = len(cell["books"]) if cell else 0
        payload_days.append({
            "date": d.isoformat(),
            "day": d.day,
            "weekday": d.isoweekday(),
            "seconds": int(seconds),
            "minutes": round(seconds / 60, 1),
            "hours": [round(h / 60, 1) for h in hours],
            "books": n_books,
            "hidden": max(0, n_books - LANES),
            "week": week_of_day[i],
        })

    return {
        "year": year,
        "month": month,
        "week_start": WEEK_START,
        "first_weekday": first.isoweekday(),
        "days_in_month": len(index_days),
        "weeks": len(weeks),
        "days": payload_days,
        "spans": spans,
        "max_day_seconds": int(max_day),
        "max_hour_seconds": int(max_hour),
        "total_seconds": int(sum(d["seconds"] for d in payload_days)),
    }


# ---------- 单日详情与时间轴 ----------

def build_day(con, date):
    """移植 getReadingDurationBySecond：30 秒内间隙合并，处理跨午夜接续。"""
    ts = int(dt.datetime.combine(date, dt.time.min).timestamp())
    ignorable_gap = max(30, DEFAULT_MIN_READ_SEC)

    rows = con.execute(
        "SELECT start_time, duration, id_book FROM page_stat_data "
        "WHERE duration > 0 AND start_time BETWEEN ? AND ? ORDER BY start_time",
        (ts - DEFAULT_MAX_READ_SEC - ignorable_gap,
         ts + 86399 + ignorable_gap)).fetchall()
    meta = _books_meta(con)

    per_book = {}
    order = []
    last_book_id = None
    last_finish = None
    for start_ts, duration, bid in rows:
        start = start_ts - ts
        finish = start + duration
        if finish < 0 or start > 86400:
            continue
        start = max(0, start)
        finish = min(86400, finish)
        if bid not in per_book:
            per_book[bid] = {"id": bid, "title": meta.get(bid, {}).get("title", "未命名"),
                             "periods": [], "seconds": 0, "raw_seconds": 0}
            order.append(bid)
        per_book[bid]["raw_seconds"] += duration
        periods = per_book[bid]["periods"]
        if (bid == last_book_id and last_finish is not None
                and start - last_finish <= ignorable_gap and periods):
            periods[-1]["finish"] = finish
        else:
            periods.append({"start": start, "finish": finish})
        last_book_id = bid
        last_finish = finish

    books = []
    for bid in order:
        entry = per_book[bid]
        entry["seconds"] = sum(p["finish"] - p["start"] for p in entry["periods"])
        entry["periods"].sort(key=lambda p: p["start"])
        books.append(entry)
    books.sort(key=lambda b: b["periods"][0]["start"] if b["periods"] else 0)

    total = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(duration),0) FROM page_stat_data "
        "WHERE duration > 0 AND start_time BETWEEN ? AND ?", (ts, ts + 86399)).fetchone()

    return {
        "date": date.isoformat(),
        "weekday": date.isoweekday(),
        "seconds": int(total[1]),
        "page_records": int(total[0]),
        "books": books,
        "day_start_ts": ts,
    }


# ---------- 周视图 ----------

def build_week(con, start):
    """某一周（周一起算）的聚合。

    一次查询覆盖「本周 7 天 + 上周同 7 天」，避免前端逐日拉 7 次 SSH：
    * days[] 逐日明细，带当天书目，供柱状图与明细表
    * books[] 本周书目占比
    * prev / delta_pct 上周同窗口，供环比
    """
    end = start + dt.timedelta(days=6)
    prev_start = start - dt.timedelta(days=7)
    prev_end = start - dt.timedelta(days=1)

    lo = int(dt.datetime.combine(prev_start, dt.time.min).timestamp())
    hi = int(dt.datetime.combine(end, dt.time.max).timestamp())

    rows = con.execute(
        "SELECT id_book, page, start_time, duration FROM page_stat_data "
        "WHERE duration > 0 AND start_time BETWEEN ? AND ? ORDER BY start_time",
        (lo, hi)).fetchall()
    meta = _books_meta(con)

    bucket = {}
    for bid, page, ts, duration in rows:
        d = dt.datetime.fromtimestamp(ts).date()
        cell = bucket.setdefault(d, {"seconds": 0, "records": 0, "books": {}})
        cell["seconds"] += duration
        cell["records"] += 1
        b = cell["books"].setdefault(bid, {"seconds": 0, "records": 0})
        b["seconds"] += duration
        b["records"] += 1

    def window_total(from_d, to_d):
        seconds = records = active = 0
        cursor = from_d
        while cursor <= to_d:
            cell = bucket.get(cursor)
            if cell:
                seconds += cell["seconds"]
                records += cell["records"]
                if cell["seconds"] > 0:
                    active += 1
            cursor += dt.timedelta(days=1)
        return seconds, records, active

    this_seconds, this_records, this_active = window_total(start, end)
    prev_seconds, prev_records, prev_active = window_total(prev_start, prev_end)

    # 上周逐日，供前端在柱状图上画对照虚线
    prev_days = []
    for i in range(7):
        d = prev_start + dt.timedelta(days=i)
        cell = bucket.get(d)
        seconds = cell["seconds"] if cell else 0
        prev_days.append({
            "date": d.isoformat(),
            "weekday": d.isoweekday(),
            "seconds": int(seconds),
            "minutes": round(seconds / 60.0, 1),
        })

    days = []
    max_day = 1
    for i in range(7):
        d = start + dt.timedelta(days=i)
        cell = bucket.get(d)
        seconds = cell["seconds"] if cell else 0
        max_day = max(max_day, seconds)
        books_today = []
        if cell:
            for bid, acc in sorted(cell["books"].items(),
                                   key=lambda kv: kv[1]["seconds"], reverse=True):
                books_today.append({
                    "id": bid,
                    "title": meta.get(bid, {}).get("title", "未命名"),
                    "seconds": int(acc["seconds"]),
                    "records": int(acc["records"]),
                })
        days.append({
            "date": d.isoformat(),
            "weekday": d.isoweekday(),
            "seconds": int(seconds),
            "minutes": round(seconds / 60, 1),
            "records": int(cell["records"]) if cell else 0,
            "books": books_today,
        })

    # 本周书目占比
    week_books = {}
    for d in range(7):
        cell = bucket.get(start + dt.timedelta(days=d))
        if not cell:
            continue
        for bid, acc in cell["books"].items():
            entry = week_books.setdefault(bid, {"seconds": 0, "records": 0, "days": set()})
            entry["seconds"] += acc["seconds"]
            entry["records"] += acc["records"]
            entry["days"].add(d)
    books = []
    for bid, acc in sorted(week_books.items(), key=lambda kv: kv[1]["seconds"], reverse=True):
        books.append({
            "id": bid,
            "title": meta.get(bid, {}).get("title", "未命名"),
            "seconds": int(acc["seconds"]),
            "records": int(acc["records"]),
            "days": len(acc["days"]),
            "share": round(acc["seconds"] * 100.0 / this_seconds, 1) if this_seconds else 0,
        })

    iso = start.isocalendar()
    delta_pct = None
    if prev_seconds:
        delta_pct = round((this_seconds - prev_seconds) * 100.0 / prev_seconds, 1)

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "iso_year": iso[0],
        "iso_week": iso[1],
        "week_start": WEEK_START,
        "is_current": start <= dt.date.today() <= end,
        "overview": {
            "seconds": int(this_seconds),
            "avg_minutes": round(this_seconds / 60.0 / this_active, 1) if this_active else 0,
            "records": int(this_records),
            "active_days": this_active,
            "total_days": 7,
            "book_count": len(books),
        },
        "prev": {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
            "seconds": int(prev_seconds),
            "records": int(prev_records),
            "active_days": prev_active,
            "days": prev_days,
        },
        "delta_pct": delta_pct,
        "days": days,
        "books": books,
        "max_day_seconds": int(max_day),
    }


def week_start_of(date):
    """把任意一天规整到所在周的周一。"""
    return date - dt.timedelta(days=date.weekday())


# ---------- 洞察 ----------

SESSION_GAP_SEC = 15 * 60     # 相邻两次翻页间隔超过它，就算新的一次阅读
SESSION_PERIODS = {
    "today": "今天",
    "7d": "近 7 天",
    "30d": "近 30 天",
    "month": "本月",
    "all": "全部",
}


def _merge_sessions(rows):
    """把翻页记录合成「一次阅读」：相邻两页间隔不超过 SESSION_GAP_SEC 就算同一次。

    rows 须按 start_time 升序；中途换书不打断一次阅读。
    end 取最后一页的开始时间加它的 duration，即合上书的大致时刻。
    """
    sessions = []
    for bid, _page, ts, duration in rows:
        if sessions and ts - sessions[-1]["end"] <= SESSION_GAP_SEC:
            s = sessions[-1]
            s["end"] = max(s["end"], ts + duration)
            s["seconds"] += duration
            s["records"] += 1
            if bid not in s["books"]:
                s["books"].append(bid)
        else:
            sessions.append({"start": ts, "end": ts + duration, "seconds": duration,
                             "records": 1, "books": [bid]})
    return sessions


def _prev_window(range_key, now):
    """与当前范围同口径的上一段完整区间：今天对昨天、本周对上周、本月对上月、今年对去年。"""
    today = now.date()
    if range_key == "today":
        return today - dt.timedelta(days=1), today - dt.timedelta(days=1)
    if range_key == "week":
        monday = today - dt.timedelta(days=today.weekday())
        return monday - dt.timedelta(days=7), monday - dt.timedelta(days=1)
    if range_key == "month":
        prev_last = today.replace(day=1) - dt.timedelta(days=1)
        return prev_last.replace(day=1), prev_last
    if range_key == "year":
        return dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31)
    return None


def _fact_window(hour_sum, total):
    """最集中的时段：能覆盖一半阅读的最短连续小时窗口，可跨午夜。"""
    for length in range(1, 13):
        sums = [sum(hour_sum[(start + k) % 24] for k in range(length)) for start in range(24)]
        best = max(sums)
        if best * 2 >= total:
            start = sums.index(best)
            end = (start + length - 1) % 24
            return {"kind": "window", "label": "读得最集中的时段", "start": start, "end": end,
                    "hours": length, "share": round(best * 100.0 / total, 1)}
    peak = max(range(24), key=lambda h: hour_sum[h])
    return {"kind": "peak", "label": "读得最多的钟点", "hour": peak,
            "share": round(hour_sum[peak] * 100.0 / total, 1)}


def _fact_speed(scoped):
    """速度最快的一天，只看读满 10 分钟的日子，避免几条记录算出离谱的页速。"""
    per_day = {}
    for _bid, _page, ts, duration in scoped:
        acc = per_day.setdefault(dt.datetime.fromtimestamp(ts).date(), [0, 0])
        acc[0] += duration
        acc[1] += 1
    cands = [(v[1] / (v[0] / 3600.0), d, v) for d, v in per_day.items() if v[0] >= 600]
    if not cands:
        return {"kind": "none", "label": "速度最快的一天", "note": "还没有哪天读满 10 分钟"}
    speed, day, v = max(cands)
    return {"kind": "speed_day", "label": "速度最快的一天", "date": day.isoformat(),
            "speed": round(speed, 1), "seconds": int(v[0]), "records": int(v[1])}


def build_insights(con, range_key="all"):
    """规律与节奏：区间小结、月度趋势、星期 × 时段热力图、几条自动结论。

    小结、热力图与结论跟随时间范围；月度趋势固定为近 12 个月，
    落在范围内的月份高亮，方便对照。
    """
    rows = _page_rows(con)
    meta = _books_meta(con)
    now = dt.datetime.now()
    cutoff = _cutoff(range_key, now)
    cutoff_ts = cutoff.timestamp() if cutoff else None
    scoped = [r for r in rows if cutoff_ts is None or r[2] >= cutoff_ts]

    total = sum(r[3] for r in scoped)
    days = {dt.datetime.fromtimestamp(r[2]).date() for r in scoped}
    book_ids = {r[0] for r in scoped}

    prev = _prev_window(range_key, now)
    prev_seconds = None
    delta_pct = None
    if prev:
        lo = int(dt.datetime.combine(prev[0], dt.time.min).timestamp())
        hi = int(dt.datetime.combine(prev[1], dt.time.max).timestamp())
        prev_seconds = sum(r[3] for r in rows if lo <= r[2] <= hi)
        if prev_seconds:
            delta_pct = round((total - prev_seconds) * 100.0 / prev_seconds, 1)

    sessions = _merge_sessions(scoped)
    longest = None
    if sessions:
        top = max(sessions, key=lambda s: s["seconds"])
        begin = dt.datetime.fromtimestamp(top["start"])
        finish = dt.datetime.fromtimestamp(top["end"])
        only = top["books"][0] if len(top["books"]) == 1 else None
        longest = {
            "seconds": int(top["seconds"]),
            "date": begin.date().isoformat(),
            "start": begin.strftime("%H:%M"),
            "end": finish.strftime("%H:%M"),
            "books": len(top["books"]),
            "title": meta.get(only, {}).get("title") if only is not None else None,
        }

    grid = [[0] * 24 for _ in range(7)]
    hour_sum = [0] * 24
    weekday_sum = [0] * 7
    for _bid, _page, ts, duration in scoped:
        moment = dt.datetime.fromtimestamp(ts)
        grid[moment.weekday()][moment.hour] += duration
        hour_sum[moment.hour] += duration
        weekday_sum[moment.weekday()] += duration
    heat = [[round(grid[w][h] / 60.0, 1) for h in range(24)] for w in range(7)]

    month_seconds, month_days = {}, {}
    for _bid, _page, ts, duration in rows:
        moment = dt.datetime.fromtimestamp(ts)
        key = (moment.year, moment.month)
        month_seconds[key] = month_seconds.get(key, 0) + duration
        month_days.setdefault(key, set()).add(moment.date())
    monthly = []
    index = now.year * 12 + now.month - 1
    for i in range(11, -1, -1):
        year, month0 = divmod(index - i, 12)
        month = month0 + 1
        last = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
        monthly.append({
            "month": "{:04d}-{:02d}".format(year, month),
            "seconds": int(month_seconds.get((year, month), 0)),
            "days": len(month_days.get((year, month), ())),
            "in_range": cutoff is None or last >= cutoff.date(),
        })

    facts = []
    if total:
        facts.append(_fact_window(hour_sum, total))
        if cutoff is None or (now.date() - cutoff.date()).days + 1 >= 7:
            ranked = sorted(range(7), key=lambda w: weekday_sum[w], reverse=True)
            top_days = [w for w in ranked[:2] if weekday_sum[w] > 0]
            facts.append({
                "kind": "weekdays", "label": "读得最多的日子", "weekdays": top_days,
                "share": round(sum(weekday_sum[w] for w in top_days) * 100.0 / total, 1),
            })
        facts.append(_fact_speed(scoped))
        acc = {}
        for bid, _page, _ts, duration in scoped:
            acc[bid] = acc.get(bid, 0) + duration
        top_book = max(acc, key=acc.get)
        facts.append({
            "kind": "top_book", "label": "读得最多的一本书", "id": top_book,
            "title": meta.get(top_book, {}).get("title", "未命名"),
            "seconds": int(acc[top_book]), "share": round(acc[top_book] * 100.0 / total, 1),
        })

    return {
        "range": range_key,
        "range_label": RANGE_LABELS.get(range_key, range_key),
        "range_start": cutoff.date().isoformat() if cutoff else None,
        "overview": {
            "seconds": int(total),
            "records": len(scoped),
            "active_days": len(days),
            "books": len(book_ids),
            "prev_seconds": int(prev_seconds) if prev_seconds is not None else None,
            "delta_pct": delta_pct,
            "session_count": len(sessions),
            "avg_session_seconds": round(total / len(sessions)) if sessions else 0,
            "longest": longest,
            "speed": round(len(scoped) / (total / 3600), 1) if total else 0,
        },
        "monthly": monthly,
        "heatmap": {"data": [[h, w, heat[w][h]] for w in range(7) for h in range(24)],
                    "max_minutes": max(max(r) for r in heat)},
        "facts": facts,
    }


# ---------- 阅读记录（一次阅读的清单） ----------

def build_sessions(con, period="7d", book=None, limit=500, now=None):
    """一段时间内的「一次阅读」，最新在前。limit 为 None 表示不截断。"""
    now = now or dt.datetime.now()
    today = now.date()
    since = None
    if period == "today":
        since = today
    elif period == "7d":
        since = today - dt.timedelta(days=6)
    elif period == "30d":
        since = today - dt.timedelta(days=29)
    elif period == "month":
        since = today.replace(day=1)
    lo = int(dt.datetime.combine(since, dt.time.min).timestamp()) if since else 1
    hi = int(now.timestamp())

    sql = ("SELECT id_book, page, start_time, duration FROM page_stat_data "
           "WHERE duration > 0 AND start_time BETWEEN ? AND ?")
    args = [lo, hi]
    if book is not None:
        sql += " AND id_book = ?"
        args.append(book)
    rows = con.execute(sql + " ORDER BY start_time", args).fetchall()

    meta = _books_meta(con)
    sessions = _merge_sessions(rows)
    sessions.reverse()

    items = []
    for s in sessions[:limit] if limit else sessions:
        begin = dt.datetime.fromtimestamp(s["start"])
        finish = dt.datetime.fromtimestamp(s["end"])
        items.append({
            "start": int(s["start"]),
            "end": int(s["end"]),
            "date": begin.date().isoformat(),
            "start_time": begin.strftime("%H:%M"),
            "end_time": finish.strftime("%H:%M"),
            "seconds": int(s["seconds"]),
            "records": int(s["records"]),
            "books": [{"id": b, "title": meta.get(b, {}).get("title", "未命名")}
                      for b in s["books"]],
        })

    # 筛选下拉用：这段时间里出现过的书，按读得多少排。不受书筛选影响，
    # 否则换了时间段，正选着的那本书会从下拉里消失。
    books = [{"id": bid, "title": meta.get(bid, {}).get("title", "未命名")}
             for (bid,) in con.execute(
                 "SELECT id_book FROM page_stat_data "
                 "WHERE duration > 0 AND start_time BETWEEN ? AND ? "
                 "GROUP BY id_book ORDER BY SUM(duration) DESC", (lo, hi))]
    if book is not None and all(b["id"] != book for b in books):
        books.insert(0, {"id": book, "title": meta.get(book, {}).get("title", "未命名")})

    return {
        "period": period,
        "range_label": SESSION_PERIODS.get(period, period),
        "since": since.isoformat() if since else None,
        "total": len(sessions),
        "truncated": limit is not None and len(sessions) > len(items),
        "seconds": int(sum(s["seconds"] for s in sessions)),
        "records": int(sum(s["records"] for s in sessions)),
        "sessions": items,
        "books": books,
    }


def sessions_csv(data):
    """把 build_sessions 的结果写成 CSV。带 BOM，Excel 直接打开中文不乱码。"""
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["日期", "开始", "结束", "分钟", "页数", "书名"])
    for s in data["sessions"]:
        writer.writerow([
            s["date"], s["start_time"], s["end_time"],
            round(s["seconds"] / 60.0, 1), s["records"],
            "、".join(b["title"] for b in s["books"]),
        ])
    return buf.getvalue()


# ---------- 空数据 ----------

def empty_snapshot(range_key="all"):
    return {
        "range": range_key,
        "range_label": RANGE_LABELS.get(range_key, range_key),
        "range_start": None,
        "overview": {
            "total_seconds": 0, "total_books": 0, "page_records": 0,
            "active_days": 0, "current_streak": 0, "longest_streak": 0,
            "avg_minutes": 0, "speed": 0, "last_read": None,
        },
        "daily": [], "hourly": [0] * 24, "weekday": [0] * 7,
        "books": [], "all_time_seconds": 0,
        "fetched_at": 0, "age_sec": 0,
    }


def empty_calendar(year, month):
    return {
        "year": year, "month": month, "week_start": WEEK_START,
        "first_weekday": dt.date(year, month, 1).isoweekday(),
        "days_in_month": 0, "weeks": 0, "days": [], "spans": [],
        "max_day_seconds": 1, "max_hour_seconds": 1, "total_seconds": 0,
    }


def empty_day(date):
    return {
        "date": date.isoformat(), "weekday": date.isoweekday(),
        "seconds": 0, "page_records": 0, "books": [], "day_start_ts": 0,
    }


def empty_week(start):
    end = start + dt.timedelta(days=6)
    iso = start.isocalendar()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "iso_year": iso[0],
        "iso_week": iso[1],
        "week_start": WEEK_START,
        "is_current": start <= dt.date.today() <= end,
        "overview": {
            "seconds": 0, "avg_minutes": 0, "records": 0,
            "active_days": 0, "total_days": 7, "book_count": 0,
        },
        "prev": {
            "start": (start - dt.timedelta(days=7)).isoformat(),
            "end": (start - dt.timedelta(days=1)).isoformat(),
            "seconds": 0, "records": 0, "active_days": 0,
            "days": [
                {
                    "date": (start - dt.timedelta(days=7 - i)).isoformat(),
                    "weekday": (start - dt.timedelta(days=7 - i)).isoweekday(),
                    "seconds": 0, "minutes": 0.0,
                }
                for i in range(7)
            ],
        },
        "delta_pct": None,
        "days": [
            {
                "date": (start + dt.timedelta(days=i)).isoformat(),
                "weekday": (start + dt.timedelta(days=i)).isoweekday(),
                "seconds": 0, "minutes": 0.0, "records": 0, "books": [],
            }
                for i in range(7)
            ],
        "books": [],
        "max_day_seconds": 1,
    }


def empty_insights(range_key="all"):
    return {
        "range": range_key,
        "range_label": RANGE_LABELS.get(range_key, range_key),
        "range_start": None,
        "overview": {
            "seconds": 0, "records": 0, "active_days": 0, "books": 0,
            "prev_seconds": None, "delta_pct": None,
            "session_count": 0, "avg_session_seconds": 0, "longest": None, "speed": 0,
        },
        "monthly": [], "heatmap": {"data": [], "max_minutes": 0}, "facts": [],
    }


def empty_sessions(period="7d"):
    return {
        "period": period, "range_label": SESSION_PERIODS.get(period, period),
        "since": None, "total": 0, "truncated": False,
        "seconds": 0, "records": 0, "sessions": [], "books": [],
    }
