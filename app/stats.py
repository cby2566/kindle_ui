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
import datetime as dt
import sqlite3
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


class StatsCache:
    """本地统计库缓存。设备不可达时退化为只读缓存，保证离线也能看历史。"""

    def __init__(self):
        self.db_path = config.CACHE_DIR / "statistics.sqlite3"
        self.fetched_at = self._file_mtime()
        self.offline = False
        self.source = "cache"       # device = 本次真的连上设备拿了新库
        self.last_error = None

    def _file_mtime(self):
        try:
            return self.db_path.stat().st_mtime
        except OSError:
            return 0.0

    def _clear_sidecars(self):
        """清掉 -wal / -shm / -journal。

        下载会整体覆盖主库文件，若旧的 WAL 还留着，SQLite 会把过期的
        增量回放到新库上，轻则读数错乱、重则报 malformed。
        设备侧 KOReader 每次 insertDB 后都会关连接触发 checkpoint，
        实测 statistics.sqlite3-wal 恒为 0 字节，所以只取主库足够。
        """
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                Path(str(self.db_path) + suffix).unlink()
            except OSError:
                pass

    def has_cache(self):
        return self.db_path.exists() and self.db_path.stat().st_size > 0

    def ensure(self, kindle, max_age=90, force=False):
        """拉取统计库。

        返回 (path, offline)。设备连不上但本地有缓存时不抛异常，
        而是标记 offline=True 继续用缓存，历史数据照样能看。
        """
        fresh = self.has_cache() and (time.time() - self.fetched_at) < max_age
        if fresh and not force:
            self.offline = False
            self.source = "cache"
            return self.db_path

        # 已有缓存时缩短连接超时，避免离线启动干等
        timeout = 4 if self.has_cache() else 8
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            kindle.download("/mnt/us/koreader/settings/statistics.sqlite3",
                            self.db_path, timeout=timeout)
        except Exception as exc:
            if not self.has_cache():
                raise DeviceError(
                    "设备未连接，且本地还没有统计缓存。请先连上设备读取一次，"
                    "之后即使断开也能查看历史数据。"
                ) from exc
            self.offline = True
            self.source = "cache"
            self.last_error = str(exc) or exc.__class__.__name__
            self.fetched_at = self._file_mtime()
            return self.db_path

        self.fetched_at = time.time()
        self.offline = False
        self.source = "device"
        self.last_error = None
        self._clear_sidecars()
        return self.db_path

    def _connect(self):
        return sqlite3.connect("file:{}?mode=ro".format(self.db_path.as_posix()), uri=True)

    def snapshot(self, range_key="all"):
        if not self.db_path.exists():
            return empty_snapshot(range_key)
        try:
            con = self._connect()
            try:
                data = build_snapshot(con, range_key)
            finally:
                con.close()
        except sqlite3.Error:
            return empty_snapshot(range_key)
        data["fetched_at"] = int(self.fetched_at)
        data["age_sec"] = int(time.time() - self.fetched_at)
        return data

    def calendar(self, year, month):
        if not self.db_path.exists():
            return empty_calendar(year, month)
        con = self._connect()
        try:
            return build_calendar(con, year, month)
        finally:
            con.close()

    def day(self, date):
        if not self.db_path.exists():
            return empty_day(date)
        con = self._connect()
        try:
            return build_day(con, date)
        finally:
            con.close()

    def week(self, start):
        if not self.db_path.exists():
            return empty_week(start)
        con = self._connect()
        try:
            return build_week(con, start)
        finally:
            con.close()


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
