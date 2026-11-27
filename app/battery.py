#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电池统计：把 KOReader batterystat 插件攒的累计量整理成「充电周期」时间线。

数据源、口径与设计取舍见 docs/battery-module.md，这里只列实现要点：

  * lua 里的 time / timestamp 是微秒单位的单调时钟（boottime，suspend 期不走），
    绝不当 Unix 时间用；绝对时间只用本机采集时刻 ts 锚定，误差=采样间隔。
  * 插件在充电开始时整组 reset，discharging 从头计（关机充电也被 init() 检测到）。
    因此「相邻快照 discharging_us 下降」是唯一可靠的周期边界信号，
    不需要关心充电发生在开机还是关机状态。
  * statistics.sqlite3 是设备端文件的整份副本、每次同步被 os.replace 整体覆盖，
    绝不能往里写本地数据；电池数据存自己的 data/cache/battery.sqlite3，只增不改。
  * 所有对 battery.sqlite3 的读写都在进程内一把锁下进行：数据量小（几百行），
    简单可靠比并发吞吐重要。statistics.sqlite3 那套 RWLock 是为了「替换文件时
    不被读请求占着句柄」，这里没有文件替换，用不上。
"""
import collections
import datetime as dt
import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import config
from .device import DeviceError

DB_PATH = config.CACHE_DIR / "battery.sqlite3"

REMOTE_LUA = "/mnt/us/koreader/settings/battery_stats.lua"
POWER_SUPPLY = "/sys/class/power_supply/max77696-battery"
# 文档 §1.3：temp / current / cycle_count / charge_full_design 量纲尚未标定，
# 照抓照存但不展示、不参与计算；等标定后历史数据还在，可以直接补算
SYSFS_FIELDS = (
    "capacity", "status", "charge_now", "charge_full", "charge_full_design",
    "cycle_count", "temp", "voltage_now", "current_now",
)

# 一次握手拿全（busybox 兼容写法，已在设备上验证）
SAMPLE_CMD = "\n".join(
    ["echo '@@uptime'; cat /proc/uptime"]
    + ["echo '@@sys'; for f in " + " ".join(SYSFS_FIELDS) + "; do "
       "echo \"$f=$(cat {}/$f 2>/dev/null)\"; done".format(POWER_SUPPLY)]
    + ["echo '@@lua'; cat {} 2>/dev/null".format(REMOTE_LUA),
       "echo '@@old'; cat {}.old 2>/dev/null".format(REMOTE_LUA)]
)

SAMPLE_THROTTLE_SEC = 120      # 距上次采集尝试（含失败）不足它，不连设备
DEDUP_WINDOW_SEC = 600         # 指纹相同且距上次入库不足它，不重复入库
CONSISTENCY_TOL = 0.05         # |awake+sleeping-discharging|/discharging 超过 5% 不自洽

# ---------------- 解析器 ----------------
# 不引入 Lua 解释器：正则容错解析。字段缺失为 None；数值 0 与缺失严格区分。

_FIELD_RE = re.compile(r'\["(\w+)"\]\s*=\s*\{([^}]*)\}')
_NUM_RE = re.compile(r'\["(percentage|time|timestamp)"\]\s*=\s*(-?\d+)')

# lua 六组键。前四组 = percentage + time（累计量）；
# 后两组 = percentage + timestamp（未累计的状态点）。time/timestamp 都是微秒。
LUA_GROUPS = ("awake", "sleeping", "charging", "discharging",
              "awake_state", "charging_state")


def parse_battery_stats(text):
    """解析 battery_stats.lua 原文。

    返回 {组名: {"pct": int|None, "us": int|None}}；原文为空或什么都没
    解析出来时返回 None，由调用方决定要不要记一条 consistent=0。
    """
    if not text or not text.strip():
        return None
    result = {}
    for m in _FIELD_RE.finditer(text):
        name, body = m.group(1), m.group(2)
        entry = {}
        for nm in _NUM_RE.finditer(body):
            if nm.group(1) == "percentage":
                entry["pct"] = int(nm.group(2))
            else:                        # time / timestamp
                entry["us"] = int(nm.group(2))
        result[name] = entry
    return result or None


def check_consistency(parsed):
    """自洽校验：awake + sleeping ≈ discharging（同一周期内三组同步累计）。

    discharging 为 0 或缺失（刚被充电重置）时不校验，视为自洽。
    不自洽 = 文件结构变了或单位假设错了 → 入库置 consistent=0。
    """
    discharging = (parsed or {}).get("discharging") or {}
    total_us = discharging.get("us")
    if not total_us:
        return True
    awake_us = ((parsed or {}).get("awake") or {}).get("us") or 0
    sleeping_us = ((parsed or {}).get("sleeping") or {}).get("us") or 0
    return abs(awake_us + sleeping_us - total_us) <= total_us * CONSISTENCY_TOL


def fingerprint(text, source):
    return "{}:{}".format(source, hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest())


def split_blocks(out):
    """把 SAMPLE_CMD 的输出按 @@ 标记切块（device.status() 的同款约定）。"""
    blocks = {}
    key = None
    for line in out.splitlines():
        if line.startswith("@@"):
            key = line[2:].strip()
            blocks[key] = []
        elif key is not None:
            blocks[key].append(line)
    return blocks


def parse_sysfs(lines):
    """解析 `capacity=72` 形式的行。空值（2>/dev/null 吞掉）→ None。"""
    values = {}
    for line in lines:
        if "=" not in line:
            continue
        name, _, raw = line.partition("=")
        name, raw = name.strip(), raw.strip()
        if name not in SYSFS_FIELDS:
            continue
        if name == "status":
            values[name] = raw or None
        else:
            try:
                values[name] = int(raw)
            except ValueError:
                values[name] = None
    return values


def parse_uptime(line):
    # 本机探针实测这台设备的 /proc/uptime 有 4 列，只有第一列是 uptime
    try:
        return float(line.split()[0])
    except (ValueError, IndexError):
        return None


# ---------------- 建库 ----------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id              INTEGER PRIMARY KEY,
    ts              INTEGER NOT NULL,   -- 采集时刻（本机 Unix 秒）
    source          TEXT    NOT NULL,   -- main | old
    fingerprint     TEXT    NOT NULL,   -- source + 原始内容 sha1，用于去重
    device_uptime   REAL,               -- /proc/uptime 第一列，校验单调时钟用
    capacity        INTEGER,
    status          TEXT,
    charge_now      INTEGER,
    charge_full     INTEGER,
    charge_full_design INTEGER,         -- 量纲未标定，只存不算（docs §1.3）
    cycle_count     INTEGER,
    temp            INTEGER,
    voltage_now     INTEGER,
    current_now     INTEGER,
    awake_pct       INTEGER, awake_us       INTEGER,
    sleeping_pct    INTEGER, sleeping_us    INTEGER,
    charging_pct    INTEGER, charging_us    INTEGER,
    discharging_pct INTEGER, discharging_us INTEGER,
    awake_state_pct    INTEGER, awake_state_us    INTEGER,
    charging_state_pct INTEGER, charging_state_us INTEGER,
    consistent      INTEGER NOT NULL DEFAULT 1,
    raw             TEXT                -- 原始文件全文，解析规则修正后可回放
);
CREATE INDEX IF NOT EXISTS idx_snapshot_ts ON snapshot(ts);
CREATE INDEX IF NOT EXISTS idx_snapshot_fp ON snapshot(fingerprint);

-- 文档 §3.2 表结构之外补一列 discharge_pct：周期总耗电在「耗电%/日均%」
-- 列都要用，awake_pct + sleeping_pct 之和替代不了它（四舍五入与充电打断
-- 会让两侧错位），闭合时直接落 discharging 的累计值。
CREATE TABLE IF NOT EXISTS cycle (
    id           INTEGER PRIMARY KEY,
    start_ts     INTEGER NOT NULL,   -- 周期起点（采样时刻，误差=采样间隔）
    end_ts       INTEGER,            -- 周期终点（重置前最后一次采样时刻，上界）
    end_pct      INTEGER,            -- 周期结束时的电量
    awake_sec    INTEGER DEFAULT 0,
    sleeping_sec INTEGER DEFAULT 0,
    awake_pct    INTEGER DEFAULT 0,
    sleeping_pct INTEGER DEFAULT 0,
    discharge_pct INTEGER DEFAULT 0,
    charged_pct  INTEGER DEFAULT 0,  -- 该周期内充电补回的百分比（充电段已知时）
    charge_sec   INTEGER DEFAULT 0,
    snapshots    INTEGER DEFAULT 0,
    boundary     TEXT,               -- truncated=被充电打断闭合 | sampled=进行中
    closed       INTEGER DEFAULT 0
);
"""

# rebuild 读取的快照字段（顺序与 _SNAP_KEYS 一一对应）
_SNAP_KEYS = ("ts", "capacity", "status",
              "awake_pct", "awake_us", "sleeping_pct", "sleeping_us",
              "charging_pct", "charging_us", "discharging_pct", "discharging_us")
_SNAP_SELECT = ("SELECT " + ", ".join(_SNAP_KEYS) +
                " FROM snapshot WHERE source='main' ORDER BY ts, id")


# ---------------- 周期重建 ----------------

def _sec(delta_us):
    """微秒整秒差。None → 0；负数（不该出现的数据倒退）截 0。"""
    if delta_us is None:
        return 0
    return max(0, int(delta_us) // 1000000)


def _d_us(row, key):
    """int|None → int（None 按 0），供单调累计差值用。"""
    return row.get(key) or 0


def _delta_pct(prev, curr):
    """int 百分比差：None 按 0 参与；负数截 0。"""
    return max(0, (curr or 0) - (prev or 0))


def rebuild_cycles(con):
    """从 snapshot 全量重算 cycle 表（先清空）。sample 落库后总会调它，
    所以周期判定规则只有这一处实现，不存在「增量/全量」两套逻辑漂移。

    判定（docs §4.3）——对升序相邻两行 P（上一次）与 V（这一次）：
      V.discharging_us >= P.discharging_us → 同周期：
          awake/sleeping 按微秒差累加，discharge_pct 跟踪最新累计值；
          charging_us 若仍在增长（插着电被连采）同样按差累加
      V.discharging_us <  P.discharging_us → 其间发生过充电重置：
          以 P 闭合上一周期（end_ts = P.ts 上界，boundary=truncated）
          从 V 开新周期；V.charging_pct>0 → 充电段已被采到，记入
          charged_pct / charge_sec

    charging_pct 的生命周期（KOReader 源码核对）：插电清零 → 充电期间累加
    → 拔电保留 → 挂到下次插电。所以「紧跟重置边界的 V」读到的 charging_pct
    必属刚结束那段充电；常规放电采样不会走到开新周期分支，陈旧值混不进来。
    唯一例外是库的第一行：无从判断它是周期中途还是起点，充电底数只能放弃
    （docs §4.3 未定义首行充电口径，界面标注起止按采样估算）。
    """
    con.execute("DELETE FROM cycle")
    snaps = [dict(zip(_SNAP_KEYS, row)) for row in con.execute(_SNAP_SELECT)
             if row[10] is not None]            # discharging_us 缺失的行不参与判定

    cycles = []
    cur = None
    prev = None

    for row in snaps:
        if prev is None or row["discharging_us"] >= _d_us(prev, "discharging_us"):
            if prev is None:
                cur = _new_cycle(row)
            else:
                # ---- 同周期：累加 P→V 增量 ----
                cur["awake_sec"] += _sec(_d_us(row, "awake_us") - _d_us(prev, "awake_us"))
                cur["sleeping_sec"] += _sec(_d_us(row, "sleeping_us") - _d_us(prev, "sleeping_us"))
                cur["awake_pct"] += _delta_pct(prev.get("awake_pct"), row.get("awake_pct"))
                cur["sleeping_pct"] += _delta_pct(prev.get("sleeping_pct"), row.get("sleeping_pct"))
                if _d_us(row, "charging_us") > _d_us(prev, "charging_us"):
                    # 跨着充电段连采（插着电过夜、期间被采到）的场景
                    cur["charge_sec"] += _sec(_d_us(row, "charging_us") - _d_us(prev, "charging_us"))
                    cur["charged_pct"] += _delta_pct(prev.get("charging_pct"), row.get("charging_pct"))
                cur["snapshots"] += 1
                cur["discharge_pct"] = _d_us_val(row)      # 跟踪最新累计值
        else:
            # ---- 重置边界：闭合旧周期，开新周期 ----
            cur["end_ts"] = prev["ts"]
            cur["end_pct"] = prev["capacity"]
            cur["boundary"] = "truncated"
            cur["closed"] = 1
            cur["discharge_pct"] = prev.get("discharging_pct") or 0
            cycles.append(cur)

            cur = _new_cycle(row)
            if (row.get("charging_pct") or 0) > 0:
                # 充电段已知：V 的 charging 累计就是这段充入的百分比与时长
                cur["charged_pct"] = row["charging_pct"]
                cur["charge_sec"] = _sec(row.get("charging_us"))
                cur["_chg_base_us"] = _d_us(row, "charging_us")
                cur["_chg_base_pct"] = row["charging_pct"]
        prev = row

    if cur is not None:
        # 最后一个周期按进行中写库；_chg_* 是计算缓存，不落库
        cycles.append(cur)

    for c in cycles:
        con.execute(
            "INSERT INTO cycle (start_ts, end_ts, end_pct, awake_sec, sleeping_sec, "
            "awake_pct, sleeping_pct, discharge_pct, charged_pct, charge_sec, "
            "snapshots, boundary, closed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["start_ts"], c.get("end_ts"), c.get("end_pct"),
             c["awake_sec"], c["sleeping_sec"], c["awake_pct"], c["sleeping_pct"],
             c.get("discharge_pct") or 0, c.get("charged_pct") or 0,
             c.get("charge_sec") or 0, c["snapshots"], c.get("boundary"), c["closed"]))
    return len(cycles)


def _new_cycle(row):
    """开新周期。百分比与时长同取 lua 累计值——两组字段同源（插电时一起
    reset），只取百分比而把时长置 0，首条快照就会出现「清醒 19% / 0 分钟」
    的自相矛盾。start_ts 仍是采样时刻，「周期天数」按本机观测窗口计，
    与这里的设备口径本来就是两回事，界面已注明「约」。"""
    return {
        "start_ts": row["ts"],
        "awake_sec": _sec(row.get("awake_us")),
        "sleeping_sec": _sec(row.get("sleeping_us")),
        "awake_pct": row.get("awake_pct") or 0,
        "sleeping_pct": row.get("sleeping_pct") or 0,
        "discharge_pct": row.get("discharging_pct") or 0,
        "charged_pct": 0, "charge_sec": 0,
        "snapshots": 1, "boundary": "sampled", "closed": 0,
    }


def _d_us_val(row):
    return row.get("discharging_pct") or 0


# ---------------- 存储与接口 ----------------

class BatteryStore:
    """battery.sqlite3 的全部读写 + 采集入口。线程安全（一把大锁串行）。"""

    _lock = threading.RLock()
    _last_try_at = 0.0            # 上次尝试采集的时刻（含失败），120s 节流用

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DB_PATH

    # ---------- 基础 ----------

    def _connect(self):
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _run(self, fn):
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            con = self._connect()
            try:
                con.executescript(_SCHEMA)
                return fn(con)
            finally:
                con.commit()
                con.close()

    def _meta(self):
        row = self._run(lambda con: con.execute(
            "SELECT COUNT(*) AS n, MAX(ts) AS m FROM snapshot").fetchone())
        return {"snapshot_count": row["n"] or 0, "updated_at": row["m"]}

    # ---------- 采集 ----------

    def sample(self, kindle=None, local=False):
        """采集一次入库；节流、去重后返回本次结果。

        本地模式是红线（docs §4.2）：只读本地库，不产生任何设备连接。
        设备错误原样抛 DeviceError（server 统一转 503），由前端静默兜住。
        """
        if local:
            data = self.overview()
            data["local"] = True
            data["sampled"] = False
            return data

        with self._lock:
            if time.time() - self._last_try_at < SAMPLE_THROTTLE_SEC:
                data = self.overview()
                data["throttled"] = True
                data["sampled"] = False
                return data
            self._last_try_at = time.time()

        # SSH 在锁外做（网络慢，不该挡住并发来的本地读）；入库再回锁内
        if kindle is None:
            from . import device as dev
            kindle = dev.Kindle()
        res = kindle.run(SAMPLE_CMD, timeout=20)
        blocks = split_blocks(res["out"])
        lua_text = "\n".join(blocks.get("lua") or [])
        if not lua_text.strip():
            raise DeviceError(
                "设备上没有 battery_stats.lua（batterystat 插件未启用或 KOReader 版本较老）")
        old_text = "\n".join(blocks.get("old") or [])
        sysfs = parse_sysfs(blocks.get("sys") or [])
        uptime = parse_uptime((blocks.get("uptime") or [""])[0])
        return self.ingest(lua_text, old_text, sysfs, uptime, source_time=time.time())

    def ingest(self, lua_text, old_text, sysfs, uptime, source_time):
        """解析 → 校验 → 去重入库 → 重建周期。拆出来是为了单测不用真机。"""
        parsed = parse_battery_stats(lua_text)
        if parsed is None:
            raise DeviceError("battery_stats.lua 无法解析：内容为空或结构不再匹配")
        consistent = check_consistency(parsed)
        fp = fingerprint(lua_text, "main")

        def apply(con):
            inserted, old_inserted = 0, 0
            # 去重看库里最后一行：指纹相同且距上次入库不足窗口 → 跳过。
            # 从 DB 推导而不是内存变量：服务重启后窗口依然成立
            # 只与 main 的最后一行比：.old 行的 id 更大、指纹必然不同，
            # 拿它比对会让「内容未变的 main」被判成新数据而重复入库
            last = con.execute(
                "SELECT ts FROM snapshot WHERE source='main' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            fresh = not (last and source_time - last["ts"] < DEDUP_WINDOW_SEC)
            if fresh:
                self._insert(con, source_time, "main", fp, sysfs, uptime,
                             parsed, consistent and 1 or 0, lua_text)
                inserted = 1

            # .old 的作用：主文件被充电重置后，.old 可能留着重置前最后一次
            # 落盘（约两小时一次 flush）。只有它比主文件「更晚」时才补一行
            # 收窄周期尾部；平时它是同周期更早的旧账，绝不能进周期判定
            # （rebuild 只读 source='main'，old 行天然不参与判定，只作参考）
            if old_text.strip():
                old_parsed = parse_battery_stats(old_text)
                main_dis = _group_us(parsed, "discharging")
                old_dis = _group_us(old_parsed, "discharging")
                if main_dis is not None and old_dis is not None and old_dis > main_dis:
                    old_fp = fingerprint(old_text, "old")
                    dup = con.execute(
                        "SELECT 1 FROM snapshot WHERE fingerprint=? LIMIT 1",
                        (old_fp,)).fetchone()
                    if not dup:
                        self._insert(con, source_time, "old", old_fp, sysfs, uptime,
                                     old_parsed, check_consistency(old_parsed) and 1 or 0,
                                     old_text)
                        old_inserted = 1

            cycle_count = rebuild_cycles(con)
            total = con.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
            return inserted, old_inserted, cycle_count, total

        inserted, old_inserted, cycle_count, total = self._run(apply)
        return {
            "sampled": True,
            "inserted": inserted,
            "old_inserted": old_inserted,
            "deduped": inserted == 0,
            "cycles": cycle_count,
            "snapshot_count": total,
            "updated_at": int(source_time),
            "consistent": consistent,
        }

    def _insert(self, con, ts, source, fp, sysfs, uptime, parsed, consistent, raw):
        def g(group, key):
            return (parsed.get(group) or {}).get(key) if parsed else None

        con.execute(
            "INSERT INTO snapshot (ts, source, fingerprint, device_uptime, "
            "capacity, status, charge_now, charge_full, charge_full_design, "
            "cycle_count, temp, voltage_now, current_now, "
            "awake_pct, awake_us, sleeping_pct, sleeping_us, "
            "charging_pct, charging_us, discharging_pct, discharging_us, "
            "awake_state_pct, awake_state_us, charging_state_pct, charging_state_us, "
            "consistent, raw) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(ts), source, fp, uptime,
             sysfs.get("capacity"), sysfs.get("status"), sysfs.get("charge_now"),
             sysfs.get("charge_full"), sysfs.get("charge_full_design"),
             sysfs.get("cycle_count"), sysfs.get("temp"),
             sysfs.get("voltage_now"), sysfs.get("current_now"),
             g("awake", "pct"), g("awake", "us"),
             g("sleeping", "pct"), g("sleeping", "us"),
             g("charging", "pct"), g("charging", "us"),
             g("discharging", "pct"), g("discharging", "us"),
             g("awake_state", "pct"), g("awake_state", "us"),
             g("charging_state", "pct"), g("charging_state", "us"),
             consistent, raw))

    # ---------- 查询 ----------

    def overview(self):
        """纯本地：当前电量、当前周期摘要、耗电率、预计剩余/充满。

        剩余/充满时间照抄 KOReader（percentageRate / remainingTime /
        chargingTime，源码已核对）：rate = pct / sec；remaining = capacity /
        rate。这是与设备菜单数字对齐的验收项。
        """
        def fn(con):
            last = con.execute(
                "SELECT * FROM snapshot WHERE source='main' ORDER BY id DESC LIMIT 1").fetchone()
            cycle = con.execute(
                "SELECT * FROM cycle WHERE closed=0 ORDER BY id DESC LIMIT 1").fetchone()
            stat = con.execute(
                "SELECT COUNT(*) AS n, MAX(ts) AS m FROM snapshot").fetchone()

            out = {"snapshot_count": stat["n"] or 0, "updated_at": stat["m"],
                   "empty": last is None}
            if last is None:
                return out

            out["capacity"] = last["capacity"]
            out["status"] = last["status"]
            out["consistent"] = bool(last["consistent"])

            awake_us = last["awake_us"]
            sleeping_us = last["sleeping_us"]
            dis_us = last["discharging_us"]
            chg_us = last["charging_us"]
            rates = {
                "awake_per_hour": _per_hour(last["awake_pct"], awake_us),
                "sleeping_per_hour": _per_hour(last["sleeping_pct"], sleeping_us),
                "overall_per_hour": _per_hour(last["discharging_pct"], dis_us),
                "charging_per_hour": _per_hour(last["charging_pct"], chg_us),
            }
            if rates["awake_per_hour"] and rates["sleeping_per_hour"]:
                rates["ratio"] = round(rates["awake_per_hour"] / rates["sleeping_per_hour"], 1)
            out["rates"] = rates

            per_sec = (last["discharging_pct"] / (dis_us / 1e6)) if (
                last["discharging_pct"] and dis_us) else None
            if per_sec and last["capacity"] is not None:
                out["remaining_sec"] = int(last["capacity"] / per_sec)
            if last["status"] == "Charging":
                chg_rate = (last["charging_pct"] / (chg_us / 1e6)) if (
                    last["charging_pct"] and chg_us) else None
                if chg_rate and last["capacity"] is not None:
                    out["time_to_full_sec"] = int(max(0, 100 - last["capacity"]) / chg_rate)

            if cycle is not None:
                dur = (stat["m"] or cycle["start_ts"]) - cycle["start_ts"]
                # 不足一小时的观测窗口不声称「几天」：首次同步当天，
                # 清醒/睡眠时长来自设备累计，而本机还没真正观测过一夜
                days = round(dur / 86400.0, 1) if dur >= 3600 else None
                discharge = cycle["discharge_pct"] or 0
                out["cycle"] = {
                    "start_ts": cycle["start_ts"],
                    "days": days,
                    "awake_sec": cycle["awake_sec"],
                    "sleeping_sec": cycle["sleeping_sec"],
                    "awake_pct": cycle["awake_pct"],
                    "sleeping_pct": cycle["sleeping_pct"],
                    "discharge_pct": discharge,
                    "charged_pct": cycle["charged_pct"] or 0,
                    "charge_sec": cycle["charge_sec"] or 0,
                    "per_day_pct": round(discharge / days, 1) if days else None,
                    "snapshots": cycle["snapshots"],
                    # 文档 §4.4：起点电量按「当前 + 已耗」估算（按充满计）
                    "start_pct_est": (last["capacity"] or 0) + discharge,
                }
            return out
        return self._run(fn)

    def cycles(self, limit=30):
        def fn(con):
            rows = con.execute(
                "SELECT * FROM cycle ORDER BY start_ts DESC LIMIT ?", (limit,)).fetchall()
            out = self._meta()
            out["cycles"] = [_cycle_payload(r) for r in rows]
            return out
        return self._run(fn)

    def snapshots(self, limit=200):
        def fn(con):
            rows = con.execute(
                "SELECT id, ts, source, capacity, status, awake_pct, awake_us, "
                "sleeping_pct, sleeping_us, charging_pct, charging_us, "
                "discharging_pct, discharging_us, consistent, device_uptime "
                "FROM snapshot ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
            out = self._meta()
            out["snapshots"] = [dict(r) for r in rows]
            return out
        return self._run(fn)

    def daily(self, days=30):
        """日净耗电：相邻快照 capacity 差按秒梯度分摊到自然日。

        capacity 来自 sysfs、不受插件 reset 影响，跨周期也成立；充电日
        电量上升、净值为负。首版标注 estimated：采样稀疏时就是近似值
        （docs §2.1 的立场——不编造精度）。
        """
        def fn(con):
            since = int(time.time()) - days * 86400
            rows = con.execute(
                "SELECT ts, capacity FROM snapshot WHERE source='main' "
                "AND capacity IS NOT NULL AND ts >= ? ORDER BY ts ASC",
                (since - 86400,)).fetchall()
            out = self._meta()
            out["estimated"] = True
            out["days"] = []
            if len(rows) < 2:
                out["note"] = "样本不足：需要至少两次不同时刻的采样"
                return out

            per_day = collections.defaultdict(float)
            for prev, curr in zip(rows, rows[1:]):
                delta = (prev["capacity"] or 0) - (curr["capacity"] or 0)  # 放电为正
                span = curr["ts"] - prev["ts"]
                if span <= 0:
                    continue
                per_sec = delta / span
                t0 = prev["ts"]
                while t0 < curr["ts"]:
                    day = dt.datetime.fromtimestamp(t0).date()
                    day_end = int(dt.datetime.combine(
                        day, dt.time.max).timestamp()) + 1
                    seg = min(day_end, curr["ts"]) - t0
                    per_day[day.isoformat()] += per_sec * seg
                    t0 += seg

            today = dt.date.today()
            for i in range(days - 1, -1, -1):
                key = (today - dt.timedelta(days=i)).isoformat()
                net = per_day.get(key, 0.0)
                out["days"].append({"date": key, "net_pct": round(net, 1),
                                    "charge": net < 0})
            return out
        return self._run(fn)

    def rebuild(self):
        """管理操作：从 snapshot 全量重算 cycle（判定规则改动后的兜底）。"""
        def fn(con):
            n = rebuild_cycles(con)
            out = self._meta()
            out["cycles"] = n
            return out
        return self._run(fn)


# ---------------- 纯工具 ----------------

def _group_us(parsed, group):
    if not parsed or group not in parsed:
        return None
    return parsed[group].get("us")


def _per_hour(pct, us):
    if not pct or not us:
        return None
    return round(pct / (us / 1e6) * 3600.0, 2)


def _cycle_payload(r):
    dur = (r["end_ts"] or int(time.time())) - r["start_ts"]
    days = round(dur / 86400.0, 2) if dur >= 3600 else None
    discharge = r["discharge_pct"] or 0
    row = dict(r)
    row.pop("_chg_base_us", None)
    row.pop("_chg_base_pct", None)
    row["days"] = days
    row["per_day_pct"] = round(discharge / days, 1) if days else None
    row["closed"] = bool(r["closed"])
    return row
