#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阅读统计：把 KOReader 的 statistics.sqlite3 读成前端直接能画的形状。

库结构（DB_SCHEMA_VERSION 20221111）：
    book(id, title, authors, notes, last_open, highlights, pages, series,
         language, md5, total_read_time, total_read_pages)
    page_stat_data(id_book, page, start_time, duration, total_pages)
时间戳是 Unix 秒，由设备按本地时区记录。
"""
import datetime as dt
import sqlite3
import time

from . import config


class StatsCache:
    def __init__(self):
        self.db_path = config.CACHE_DIR / "statistics.sqlite3"
        self.fetched_at = 0.0

    def ensure(self, kindle, max_age=90, force=False):
        fresh = self.db_path.exists() and (time.time() - self.fetched_at) < max_age
        if fresh and not force:
            return self.db_path
        kindle.download("/mnt/us/koreader/settings/statistics.sqlite3", self.db_path)
        self.fetched_at = time.time()
        return self.db_path

    def payload(self):
        if not self.db_path.exists():
            return empty_payload()
        try:
            con = sqlite3.connect("file:{}?mode=ro".format(self.db_path.as_posix()), uri=True)
        except sqlite3.Error:
            return empty_payload()
        try:
            data = build(con)
        except sqlite3.Error:
            return empty_payload()
        finally:
            con.close()
        data["fetched_at"] = int(self.fetched_at)
        data["age_sec"] = int(time.time() - self.fetched_at)
        return data


def empty_payload():
    return {
        "overview": {
            "total_seconds": 0, "total_books": 0, "page_records": 0,
            "active_days": 0, "current_streak": 0, "longest_streak": 0,
            "avg_minutes": 0, "last_read": None, "this_week_seconds": 0,
        },
        "daily": [], "hourly": [0] * 24, "weekday": [0] * 7,
        "calendar": [], "books": [],
        "fetched_at": 0, "age_sec": 0,
    }


def build(con):
    cur = con.cursor()
    rows = cur.execute(
        "SELECT start_time, duration FROM page_stat_data WHERE duration > 0"
    ).fetchall()

    daily, hourly, weekday = {}, [0] * 24, [0] * 7
    total = 0
    for start, duration in rows:
        if not start or not duration or duration < 0:
            continue
        ts = dt.datetime.fromtimestamp(start)
        d = ts.date()
        daily[d] = daily.get(d, 0) + duration
        hourly[ts.hour] += duration
        weekday[ts.weekday()] += duration
        total += duration

    today = dt.date.today()
    calendar = []
    for i in range(370, -1, -1):
        d = today - dt.timedelta(days=i)
        calendar.append([d.isoformat(), round(daily.get(d, 0) / 60, 1)])

    daily_series = []
    for i in range(89, -1, -1):
        d = today - dt.timedelta(days=i)
        daily_series.append([d.isoformat(), round(daily.get(d, 0) / 60, 1)])

    active_days = len(daily)
    longest, run = 0, 0
    prev = None
    for d in sorted(daily):
        if prev and (d - prev).days == 1:
            run += 1
        else:
            run = 1
        longest = max(longest, run)
        prev = d

    streak = 0
    cursor = today if today in daily else today - dt.timedelta(days=1)
    while cursor in daily:
        streak += 1
        cursor -= dt.timedelta(days=1)

    week_start = today - dt.timedelta(days=today.weekday())
    this_week = sum(v for d, v in daily.items() if d >= week_start)

    books = []
    q = """
        SELECT b.id, b.title, b.authors, b.pages, b.last_open, b.total_read_time,
               SUM(p.duration) AS secs,
               COUNT(DISTINCT p.page) AS pages_read,
               MAX(p.start_time) AS last_ts
        FROM book b
        JOIN page_stat_data p ON p.id_book = b.id
        GROUP BY b.id
        ORDER BY secs DESC
    """
    for bid, title, authors, pages, last_open, trt, secs, pages_read, last_ts in cur.execute(q):
        progress = None
        if pages and pages > 0:
            progress = min(100.0, round(pages_read * 100.0 / pages, 1))
        books.append({
            "id": bid,
            "title": (title or "未命名").strip(),
            "authors": (authors or "").strip(),
            "seconds": int(secs or 0),
            "minutes": round((secs or 0) / 60, 1),
            "pages_read": pages_read,
            "pages_total": pages or None,
            "progress": progress,
            "last_read": int(last_ts or last_open or 0),
        })

    last_read = max((b["last_read"] for b in books), default=0)

    return {
        "overview": {
            "total_seconds": int(total),
            "total_books": len(books),
            "page_records": len(rows),
            "active_days": active_days,
            "current_streak": streak,
            "longest_streak": longest,
            "avg_minutes": round(total / 60 / active_days, 1) if active_days else 0,
            "last_read": last_read or None,
            "this_week_seconds": int(this_week),
        },
        "daily": daily_series,
        "hourly": [round(v / 60, 1) for v in hourly],
        "weekday": [round(v / 60, 1) for v in weekday],
        "calendar": calendar,
        "books": books[:40],
    }
