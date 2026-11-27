#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""battery 模块单测。

fixture 是 2026-11-27 从真实设备（KPW3 · KOReader v2026.07.1）上抓下来的
battery_stats.lua 原文——主文件与 .old 各一份；周期判定用例在此基础上构造
「重置前/重置后」的快照序列，数值自洽（awake+sleeping≈discharging），
覆盖 docs/battery-module.md §8-P0 列出的三类用例与 §9 的验收项 2、3。

跑法：py -m unittest discover tests -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import battery as battery_mod
from app.battery import (BatteryStore, check_consistency, parse_battery_stats,
                         rebuild_cycles)

# 探针 2026-11-27 实抓：主文件（awake 3.6h + sleeping 58.8h ≈ discharging 62.4h）
REAL_MAIN = """-- ./settings/battery_stats.lua
return {
    ["awake"] = {
        ["percentage"] = 14,
        ["time"] = 12933725575,
    },
    ["awake_state"] = {
        ["percentage"] = 77,
        ["timestamp"] = 228452145828,
    },
    ["charging"] = {
        ["percentage"] = 0,
        ["time"] = 0,
    },
    ["charging_state"] = {
        ["percentage"] = 77,
        ["timestamp"] = 228452152938,
    },
    ["discharging"] = {
        ["percentage"] = 16,
        ["time"] = 224547004910,
    },
    ["sleeping"] = {
        ["percentage"] = 2,
        ["time"] = 211613233638,
    },
}
"""

# 同日实抓：.old（更早一次落盘，discharging 210827s < 主文件 224547s）
REAL_OLD = """-- ./settings/battery_stats.lua
return {
    ["awake"] = {
        ["percentage"] = 8,
        ["time"] = 6265333607,
    },
    ["awake_state"] = {
        ["percentage"] = 83,
        ["timestamp"] = 214732368013,
    },
    ["charging"] = {
        ["percentage"] = 0,
        ["time"] = 0,
    },
    ["charging_state"] = {
        ["percentage"] = 83,
        ["timestamp"] = 214732374058,
    },
    ["discharging"] = {
        ["percentage"] = 10,
        ["time"] = 210827258697,
    },
    ["sleeping"] = {
        ["percentage"] = 2,
        ["time"] = 204561865616,
    },
}
"""

SYSFS = {"capacity": 72, "status": "Discharging", "charge_now": 1046,
         "charge_full": 1408, "charge_full_design": 790000, "cycle_count": 4,
         "temp": 71, "voltage_now": 3950, "current_now": -157}


class TestParser(unittest.TestCase):
    def test_real_main_text(self):
        parsed = parse_battery_stats(REAL_MAIN)
        self.assertEqual(parsed["awake"], {"pct": 14, "us": 12933725575})
        self.assertEqual(parsed["sleeping"], {"pct": 2, "us": 211613233638})
        self.assertEqual(parsed["charging"], {"pct": 0, "us": 0})       # 0 不是 None
        self.assertEqual(parsed["discharging"], {"pct": 16, "us": 224547004910})
        self.assertEqual(parsed["awake_state"], {"pct": 77, "us": 228452145828})
        self.assertEqual(parsed["charging_state"], {"pct": 77, "us": 228452152938})

    def test_real_old_text(self):
        self.assertEqual(parse_battery_stats(REAL_OLD)["discharging"]["us"], 210827258697)

    def test_missing_group_and_field(self):
        parsed = parse_battery_stats(
            'return {\n  ["awake"] = {\n    ["percentage"] = 5,\n  },\n}\n')
        self.assertEqual(parsed["awake"]["pct"], 5)
        self.assertIsNone(parsed["awake"].get("us"))
        self.assertNotIn("discharging", parsed)

    def test_empty_and_garbage(self):
        self.assertIsNone(parse_battery_stats(""))
        self.assertIsNone(parse_battery_stats("   \n"))
        self.assertIsNone(parse_battery_stats("not a lua file at all"))

    def test_consistency_real_data(self):
        # 真实数据：|3.59h + 58.78h - 62.37h| / 62.37h << 5%
        self.assertTrue(check_consistency(parse_battery_stats(REAL_MAIN)))

    def test_consistency_broken(self):
        # 人为把 sleeping 时长拉高 30 小时：|awake+sleeping-discharging|
        # 超出 5% 容差（校验公式用的是时长不是百分比，见 §4.5）
        broken = REAL_MAIN.replace('"time"] = 211613233638', '"time"] = 311613233638', 1)
        self.assertFalse(check_consistency(parse_battery_stats(broken)))

    def test_consistency_zero_discharging(self):
        # 刚被充电重置的全 0：不校验、视为自洽
        reset = 'return {\n ["discharging"] = { ["percentage"] = 0, ["time"] = 0 },\n}\n'
        self.assertTrue(check_consistency(parse_battery_stats(reset)))


class TestIngestAndDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BatteryStore(db_path=Path(self.tmp.name) / "battery.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_dedup_window(self):
        """验收 §9-P0：同指纹 10 分钟内二次采集被去重，窗口外落行。"""
        r1 = self.store.ingest(REAL_MAIN, REAL_OLD, SYSFS, 238973.26, source_time=1000)
        self.assertEqual(r1["inserted"], 1)
        r2 = self.store.ingest(REAL_MAIN, REAL_OLD, SYSFS, 238973.26, source_time=1300)
        self.assertTrue(r2["deduped"])                               # 5 分钟内：跳过
        self.assertEqual(r2["snapshot_count"], 1)
        r3 = self.store.ingest(REAL_MAIN, REAL_OLD, SYSFS, 238973.26, source_time=1700)
        self.assertEqual(r3["inserted"], 1)                          # 超窗口：允许落行
        self.assertEqual(r3["snapshot_count"], 2)

    def test_ingest_old_snapshot(self):
        """主文件被重置、.old 反而更晚时，补一条 source='old' 的行。"""
        main_now = (REAL_MAIN
                    .replace('["percentage"] = 16,', '["percentage"] = 0,', 1)
                    .replace('["time"] = 224547004910', '["time"] = 5000000', 1))
        r = self.store.ingest(main_now, REAL_OLD, SYSFS, 100.0, source_time=2000)
        self.assertEqual(r["old_inserted"], 1)
        rows = self.store.snapshots(10)["snapshots"]
        self.assertEqual(rows[0]["source"], "old")
        self.assertEqual(rows[1]["source"], "main")
        # 回归：库里已有 .old 行时，内容未变的 main 仍须被去重
        # （去重若拿全表最后一行比对，会因 old 指纹不同而重复入库）
        r2 = self.store.ingest(main_now, REAL_OLD, SYSFS, 100.0, source_time=2100)
        self.assertTrue(r2["deduped"])
        self.assertEqual(r2["snapshot_count"], 2)

    def test_ingest_rejects_unparseable(self):
        from app.device import DeviceError
        with self.assertRaises(DeviceError):
            self.store.ingest("", "", SYSFS, None, source_time=1)


class TestCycleRebuild(unittest.TestCase):
    """验收 §9-3：构造「重置前后」快照，能闭合 truncated 周期。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "battery.sqlite3"
        store = BatteryStore(db_path=self.db)
        self.con = sqlite3.connect(str(self.db))
        self.con.row_factory = sqlite3.Row
        try:
            self.con.executescript(battery_mod._SCHEMA)
        except Exception:
            self.con.close()
            self.tmp.cleanup()
            raise
        self.t = 1700000000                    # 任意基准 Unix 秒

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _snap(self, ts, discharging_us, awake_us, sleeping_us, capacity,
              discharging_pct, awake_pct, sleeping_pct, charging_pct=0,
              charging_us=0, status="Discharging"):
        self.con.execute(
            "INSERT INTO snapshot (ts, source, fingerprint, capacity, status, "
            "awake_pct, awake_us, sleeping_pct, sleeping_us, charging_pct, charging_us, "
            "discharging_pct, discharging_us, consistent, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,'x')",
            (ts, "main", "fp{}".format(ts), capacity, status,
             awake_pct, awake_us, sleeping_pct, sleeping_us,
             charging_pct, charging_us, discharging_pct, discharging_us))

    def test_reset_closes_truncated_cycle(self):
        # 周期 A：三次采样，discharging 单调增长（10min → 24h → 62.4h）。
        # 首行距 reset 已过 10 分钟：周期时长只能从首行起计，丢的这 600 秒
        # 就是「起止误差=采样间隔」（docs §4.3），断言容差按它放宽
        self._snap(self.t,        600_000_000,    550_000_000,      50_000_000, 80, 1, 1, 0)
        self._snap(self.t + 3600, 86_400_000_000, 14_000_000_000,   72_400_000_000, 60, 10, 8, 2)
        self._snap(self.t + 7200, 224_547_000_000, 14_500_000_000, 210_047_000_000, 55, 12, 10, 2)
        # 重置后充电中：discharging≈0，charging 已累计 40% / 2.4h
        self._snap(self.t + 86400, 100_000_000, 50_000_000, 50_000_000, 100,
                   0, 0, 0, charging_pct=40, charging_us=8_640_000_000,
                   status="Charging")
        # 拔电后放电
        self._snap(self.t + 90000, 180_000_000, 60_000_000, 120_000_000, 98, 2, 1, 1)

        n = rebuild_cycles(self.con)
        rows = self.con.execute("SELECT * FROM cycle ORDER BY id").fetchall()
        self.assertEqual(n, 2)

        first = rows[0]
        self.assertEqual(first["closed"], 1)
        self.assertEqual(first["boundary"], "truncated")
        self.assertEqual(first["end_ts"], self.t + 7200)     # 闭合于重置前最后一次采样
        self.assertEqual(first["end_pct"], 55)
        self.assertEqual(first["discharge_pct"], 12)
        self.assertEqual(first["awake_pct"], 10)             # 单调累计取最新值
        # awake+sleeping 与 discharging（224547s）差值 ≈ 首行前的 600s
        self.assertAlmostEqual(
            first["awake_sec"] + first["sleeping_sec"], 224_547, delta=700)

        second = rows[1]
        self.assertEqual(second["closed"], 0)                # 进行中
        self.assertEqual(second["charged_pct"], 40)          # 充电段已知
        self.assertEqual(second["charge_sec"], 8640)
        self.assertEqual(second["start_ts"], self.t + 86400)
        self.assertEqual(second["boundary"], "sampled")

    def test_same_cycle_accumulates(self):
        self._snap(self.t,        3_600_000_000, 3_000_000_000, 600_000_000, 90, 4, 3, 1)
        self._snap(self.t + 60,   7_200_000_000, 5_000_000_000, 2_200_000_000, 89, 8, 5, 3)
        rebuild_cycles(self.con)
        row = self.con.execute("SELECT * FROM cycle").fetchone()
        self.assertEqual(row["closed"], 0)
        self.assertEqual(row["awake_sec"], 5000)             # 首行 3e9 基数 + 增量 2e9
        self.assertEqual(row["sleeping_sec"], 2200)          # 首行 0.6e9 基数 + 增量 1.6e9
        self.assertEqual(row["awake_pct"], 5)                # 首行基数 3 + 增量 2
        self.assertEqual(row["discharge_pct"], 8)
        self.assertEqual(row["snapshots"], 2)

    def test_charging_reset_without_mid_sample(self):
        """常见时序：插/拔电全程没被采到，靠 discharging 下降直接判定。"""
        self._snap(self.t,        100_000_000_000, 10_000_000_000, 90_000_000_000,
                   40, 10, 1, 9)
        self._snap(self.t + 86400, 1_000_000_000, 600_000_000, 400_000_000, 96, 1, 1, 0,
                   charging_pct=55, charging_us=43_200_000_000)
        rebuild_cycles(self.con)
        rows = self.con.execute("SELECT * FROM cycle ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["boundary"], "truncated")
        self.assertEqual(rows[0]["discharge_pct"], 10)
        self.assertEqual(rows[1]["charged_pct"], 55)     # 拔电后才采，充电段整段计入


class TestQueries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BatteryStore(db_path=Path(self.tmp.name) / "battery.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_db(self):
        """验收 §9-6：空库显示空态而非报错。"""
        self.assertTrue(self.store.overview()["empty"])
        self.assertEqual(self.store.cycles()["cycles"], [])
        self.assertEqual(self.store.daily(30)["days"], [])
        self.assertIn("note", self.store.daily(30))

    def test_overview_matches_koreader(self):
        """验收 §9-1 的口径对齐：rate/remaining 与 KOReader 公式一致。"""
        self.store.ingest(REAL_MAIN, REAL_OLD, {"capacity": 72, "status": "Discharging"},
                          238973.26, source_time=1700000000)
        ov = self.store.overview()
        # rate = 16% / 62.37h ≈ 0.256 %/h；remaining = 72 / rate
        self.assertAlmostEqual(ov["rates"]["overall_per_hour"], 0.26, delta=0.02)
        self.assertEqual(ov["capacity"], 72)
        self.assertGreater(ov["remaining_sec"], 0)
        self.assertEqual(ov["cycle"]["awake_pct"], 14)
        self.assertEqual(ov["cycle"]["sleeping_pct"], 2)
        self.assertEqual(ov["cycle"]["snapshots"], 1)
        # 文档核心表达：清醒/睡眠速率的量级差
        self.assertGreater(ov["rates"]["awake_per_hour"],
                           ov["rates"]["sleeping_per_hour"] * 10)

    def test_daily_allocates_across_days(self):
        import time as _time
        t0 = int(_time.time()) - 2 * 86400    # 落在 30 天窗口内
        self.store.ingest(REAL_MAIN, "", {"capacity": 80, "status": "Discharging"},
                          None, source_time=t0)
        main2 = REAL_MAIN.replace('["percentage"] = 16', '["percentage"] = 36')
        self.store.ingest(main2, "", {"capacity": 60, "status": "Discharging"},
                          None, source_time=t0 + 86400)
        days = {d["date"]: d["net_pct"] for d in self.store.daily(30)["days"]}
        values = [v for v in days.values() if v]
        self.assertAlmostEqual(sum(values), 20.0, delta=1.0)   # 两天合计 80→60


if __name__ == "__main__":
    unittest.main(verbosity=2)
