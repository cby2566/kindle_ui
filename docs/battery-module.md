# 电池统计模块 · 开发方案

> 目标：把 KOReader 自己攒的电池累计量，整理成一条能读懂「每个充电周期是怎么用掉的」时间线。
> 只读设备，不上传、不修改、不安装任何东西。

---

## 1. 已确认的地基

以下全部来自设备实测，不是推测。

### 1.1 数据源

| 来源 | 路径 | 内容 |
|---|---|---|
| KOReader 插件 `batterystat` | `/mnt/us/koreader/settings/battery_stats.lua` | 清醒 / 睡眠 / 充电 / 放电四组累计量 |
| 上一版备份 | 同目录 `battery_stats.lua.old` | 上一次 flush 的副本，可补周期尾部 |
| sysfs | `/sys/class/power_supply/max77696-battery/` | `capacity` `status` `charge_now` `charge_full` `cycle_count` `temp` `voltage_now` `current_now` |
| 系统 | `/proc/uptime` | 单调时钟基准，用于校验 |

`battery_stats.lua` 的真实结构：

```lua
return {
    ["awake"]      = { ["percentage"] = 14, ["time"] = 12933725575 },
    ["sleeping"]   = { ["percentage"] =  2, ["time"] = 211613233638 },
    ["charging"]   = { ["percentage"] =  0, ["time"] = 0 },
    ["discharging"]= { ["percentage"] = 16, ["time"] = 224547004910 },
    ["awake_state"]    = { ["percentage"] = 77, ["timestamp"] = 228452145828 },
    ["charging_state"] = { ["percentage"] = 77, ["timestamp"] = 228452152938 },
}
```

### 1.2 三个已定死的口径

1. **`time` 与 `timestamp` 单位是微秒**。依据：清醒 3.6 h + 睡眠 58.8 h ≈ 放电总时长 62.4 h，三数自洽。
2. **`timestamp` 是单调时钟，不是 Unix 时间戳**。依据：`awake_state.timestamp / 1e6 = 228452 s`，同期设备 `/proc/uptime = 238048 s`，差值约 2.7 h 正是 suspend 期间不走的时长。
   → **绝对时间只能由本机采集时刻锚定**，lua 里的时间戳只用于算时长。
3. **充电会清零累计**。`onCharging` 触发 `reset(true, true)`，`discharging` 从头计。
   → 这是本模块最大的约束，第 3 节整节都在处理它。

### 1.3 首版不做的事（单位未标定，做了就是错的数字）

| 项 | 实测值 | 问题 |
|---|---|---|
| 电池健康度 | `charge_full=1408` / `charge_full_design=790000` | 两者量纲不一致，无法直接求比值 |
| 温度 | `temp=71` | 按 0.1°C 是 7.1°C（不合理），按 °F 是 21.7°C（合理），待室温对照标定 |
| 循环次数 | `cycle_count=4` | 数值可疑，疑似驱动未正确累计 |
| 电流 | `current_now=-287` `current_avg=-50` | 单位（µA / 0.1mA）未定 |

这些节点**照抓照存**（存入 `snapshot`），只是首版不展示、不参与计算。等有一天能标定，历史数据还在，可以直接补算。

---

## 2. 产品定位

### 2.1 以「充电周期」为单位，不以「日」为单位

采样密度受制于「什么时候打开面板」，日级曲线必然稀疏且失真。而 KOReader 的累计量本身就是**按充电周期组织**的，顺着它的结构走，才不需要编造精度。

页面回答四个问题：

1. 现在还剩多久？（当前周期 + 耗电速率）
2. 上一个周期是怎么用掉的？（清醒 vs 睡眠各占多少）
3. 历次周期对比——续航是不是在变差？（周期天数折线 + 清醒时长叠加，用以区分「电池老化」与「这周读得多」）
4. 有没有异常掉电？（单周期日均耗电显著偏高）

### 2.2 侧栏位置建议

```
阅读统计 · 洞察 · 阅读记录 · 电池 · 书库 · 设备
```

理由：前四个都是「读」的维度（读了多少、怎么读、每次读、读得费不费电），后两个是「管」的维度（文件、连接）。电池紧邻阅读记录，比挂在设备页下更像一条主线。

设备页现有的电池小环保留，但只作摘要，并加一个「查看电池统计」跳转，避免两处各写一套逻辑。

---

## 3. 数据模型

### 3.1 为什么单独建库

`data/cache/statistics.sqlite3` 是设备端文件的**整份副本**，每次同步会被覆盖，绝不能往里写本地数据。

新建 `data/cache/battery.sqlite3`，本机自有、只增不改。

### 3.2 表结构

```sql
CREATE TABLE IF NOT EXISTS snapshot (
    id              INTEGER PRIMARY KEY,
    ts              INTEGER NOT NULL,   -- 采集时刻（本机 Unix 秒）
    source          TEXT    NOT NULL,   -- main | old
    fingerprint     TEXT    NOT NULL,   -- 原始内容 sha1，用于去重
    device_uptime   REAL,               -- /proc/uptime 第一列
    capacity        INTEGER,
    status          TEXT,
    charge_now      INTEGER,
    charge_full     INTEGER,
    cycle_count     INTEGER,
    temp_raw        INTEGER,
    voltage_now     INTEGER,
    current_now     INTEGER,
    awake_pct       INTEGER, awake_us       INTEGER,
    sleeping_pct    INTEGER, sleeping_us    INTEGER,
    charging_pct    INTEGER, charging_us    INTEGER,
    discharging_pct INTEGER, discharging_us INTEGER,
    awake_state_pct    INTEGER, awake_state_us    INTEGER,
    charging_state_pct INTEGER, charging_state_us INTEGER,
    consistent      INTEGER NOT NULL DEFAULT 1,  -- 自洽校验结果
    raw             TEXT                          -- 原始文件全文
);
CREATE INDEX IF NOT EXISTS idx_snapshot_ts ON snapshot(ts);
CREATE INDEX IF NOT EXISTS idx_snapshot_fp ON snapshot(fingerprint);

CREATE TABLE IF NOT EXISTS cycle (
    id           INTEGER PRIMARY KEY,
    start_ts     INTEGER NOT NULL,   -- 周期起点（采样时刻，见 4.3 的边界说明）
    end_ts       INTEGER,
    end_pct      INTEGER,            -- 周期结束时的电量
    awake_sec    INTEGER DEFAULT 0,
    sleeping_sec INTEGER DEFAULT 0,
    awake_pct    INTEGER DEFAULT 0,
    sleeping_pct INTEGER DEFAULT 0,
    charged_pct  INTEGER DEFAULT 0,  -- 该周期结束时充电补回的百分比
    charge_sec   INTEGER DEFAULT 0,
    snapshots    INTEGER DEFAULT 0,
    boundary     TEXT,               -- exact | sampled | truncated
    closed       INTEGER DEFAULT 0
);
```

### 3.3 几条设计取舍

- **存 `raw`**：解析规则日后若有修正，可以从原文重跑，不必回设备重抓。超过 90 天的行可选择性清空 `raw`，其余字段保留。
- **存 `fingerprint`**：`battery_stats.lua` 只在 flush 时更新（实测两小时才变一次），面板频繁打开会采到完全相同的内容。指纹相同且距上次不足 10 分钟则不入库，只更新内存里的「最近见到」时刻，避免堆满重复行。
- **`cycle` 可重建**：提供 `rebuild` 操作，从 `snapshot` 全量重算，周期判定规则改动后不必担心历史脏了。

---

## 4. 采集策略

### 4.1 单次采集 = 一次 SSH，一条命令

```sh
cat /proc/uptime; echo '@@'
cat /sys/class/power_supply/max77696-battery/capacity; echo '@@'
...（capacity/status/charge_now/charge_full/cycle_count/temp/voltage_now/current_now）
echo '@@'
cat /mnt/us/koreader/settings/battery_stats.lua
echo '@@'
cat /mnt/us/koreader/settings/battery_stats.lua.old
```

一次握手拿全，约 1–2 秒。不新增依赖，复用 `app/device.py` 的 `Kindle.run()`。

### 4.2 触发点

| 时机 | 行为 |
|---|---|
| 进入电池页 | 先渲染本地数据，再后台采一次，有新数据则刷新 |
| 进入设备页（`loadStatus` 之后） | 顺手采一次 |
| 手动「立即同步」按钮 | 采一次 |
| 距上次采集 < 120 秒 | 直接返回上次结果，不连设备 |

`/api/stats` **不掺和**——统计页是最常开的页面，不该为了电池多一次往返。

**本地模式**：只读本地库，不发任何采样请求。这条是红线。

### 4.3 周期判定（核心逻辑）

设本次采样向量 `V`，上次为 `P`：

```
若 V.discharging_us >= P.discharging_us
    → 同周期，累加增量：Δ_awake = V.awake_us - P.awake_us，其余同理
    → 更新 cycle 的时长与耗电

若 V.discharging_us <  P.discharging_us
    → 发生过充电重置
    → 闭合上一周期：end_ts = P.ts（上界），boundary = 'truncated'
                    若 P 时状态为 Charging，则周期确实在充电中结束
    → 新开周期：start_ts = V.ts（下界）
                    若 P 的 charging_us > 0，说明充电段已知，记入 charged_pct / charge_sec
```

**边界精度必须如实标注**：周期起止时刻的误差 = 采样间隔。界面上一律写「约」，并在周期表下方注明「起止时间按采样时刻估算」。

`.old` 的作用：主文件被重置后，`.old` 里可能还留着重置前最后一次落盘。采集时若 `.old` 的 `discharging_us` 大于主文件的，就把 `.old` 作为一条 `source='old'` 的快照补入，用来收窄周期尾部的误差。`old` 行只参与周期闭合，不参与趋势统计。

### 4.4 起点电量不可知

`battery_stats.lua` 不记录「这一周期从百分之几开始」。只能按「充满」假设，或按首次采样的 `capacity` 反推。首版一律以 `capacity + discharging_pct` 估算起点，并标注「按充满计」。

### 4.5 失败处理

- 设备离线 / 超时：**静默保留上次数据**，页面显示「上次同步：X 前」，不弹错误遮罩（电池页不该因设备睡着而不可用）。
- 解析失败：写入 `snapshot` 时置 `consistent=0`，界面用次要样式标出该点，不中断其它数据。
- 自洽校验：`|awake_us + sleeping_us - discharging_us| / discharging_us > 5%` 即视为异常（说明文件结构变了或单位假设错了）。

---

## 5. 解析器

不引入 Lua 解释器（PC 端没有，也不该为此装东西）。写一个**容错正则解析**：

```python
FIELD = re.compile(r'\["(\w+)"\]\s*=\s*\{([^}]*)\}')
NUM   = re.compile(r'\["(percentage|time|timestamp)"\]\s*=\s*(-?\d+)')
```

要求：

- 字段缺失 → 该字段为 `None`，不抛异常
- 文件为空 / 不存在 → 返回 `None`，由调用方决定是否记一条 `consistent=0`
- 数值 0 与缺失区分开
- 单元测试用**设备上抓下来的真实文本**作 fixture，覆盖：正常、重置后（charging 非零）、缺字段、空文件

---

## 6. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/battery/sample` | 采集一次入库，返回新增快照与周期变化；120 秒内幂等 |
| GET | `/api/battery/overview` | 纯本地：当前电量、当前周期摘要、清醒/睡眠耗电率、预计剩余/充满 |
| GET | `/api/battery/cycles?limit=30` | 周期列表，倒序 |
| GET | `/api/battery/daily?days=30` | 日净耗电（相邻快照差值按时间分摊，标注 `estimated`） |
| GET | `/api/battery/snapshots?limit=200` | 原始点，供曲线与排查 |
| POST | `/api/battery/rebuild` | 从 snapshot 重算 cycle |

响应统一带 `updated_at` 与 `snapshot_count`，让前端能显示「数据截至何时」。设备错误沿用现有的 `DeviceError` → 503 约定。

`overview` 里给出的预计剩余时间，算法照抄 KOReader：

```
rate_per_sec = discharged_pct / discharging_sec
remaining    = current_pct / rate_per_sec
```

与设备上菜单里看到的数字应当一致——这是验收项之一。

---

## 7. 前端

### 7.1 视图结构（`#battery`）

```
view-battery
├─ 状态条     电量环 + 充放电状态 + 预计剩余 / 充满 + 「上次同步」 + 同步按钮
├─ 对比卡     清醒 3.9 %/h  ｜  睡眠 0.034 %/h  ｜  相差 115 倍
├─ 周期表     起止 · 天数 · 清醒时长 · 睡眠时长 · 耗电% · 充电耗时 · 日均%
├─ 趋势图     各周期「天/次充电」折线，叠加该周期清醒时长（双轴）
└─ 空态       尚无数据时：说明 + 「立即同步」
```

「相差 115 倍」这类对比是本模块的核心表达——数字本身不稀奇，**清醒与待机的量级差**才是主人会想看的。

### 7.2 与现有代码的接法

- `index.html`：nav 加 `data-view="battery"`，main 里加 `<section class="view" id="view-battery">`
- `app.js`：`VIEW_META` 加一项、`SIMPLE_VIEWS` 加 `"battery"`、`applyView()` 里加分支（进入即 `loadBattery()`）
- `style.css`：复用现有 `--panel` / `--accent` / `--radius` 等变量与 `.card` / `.kpi` / `.table` 骨架，只写电池页特有的两三处
- 图表用已有 `vendor/echarts.min.js`，不新增依赖

---

## 8. 分期

### P0 · 地基

- `app/battery.py`：解析器、自洽校验、建库、`sample()`、`cycle` 判定、`overview()`
- `app/server.py`：三个只读接口 + `sample`
- 单测：真实 fixture 的解析用例、周期判定用例（重置 / 同周期 / 缺字段）
- 验收：命令行跑 `sample` 两次，库里出现两条快照且第二次指纹相同被正确去重

### P1 · 页面

- 侧栏入口、视图骨架、状态条、对比卡、周期表
- 空态与「立即同步」
- 验收：真实数据下四个数字与设备上 KOReader 菜单一致

### P2 · 趋势

- 周期折线（天/次充电）+ 清醒时长叠加
- 日净耗电（若数据足够，否则显示「样本不足」）

### P3 · 待标定后

- 温度、电流、健康度、异常掉电提示
- 前置条件：完成 1.3 里的四项标定

---

## 9. 验收标准

1. 解析结果与设备上 KOReader「Battery statistics」菜单显示的百分比、时长一致（误差 ≤ 1 个百分点 / 1 分钟）
2. 自洽校验对真实数据的通过率 100%；人为破坏一段文本能正确置 `consistent=0`
3. 充电重置场景：构造两次「重置前后」的快照，能闭合一个 `truncated` 周期，且 `awake_sec + sleeping_sec` 与 `discharging` 差值在容差内
4. 本地模式：进入电池页不产生任何设备连接（抓包或日志断言）
5. 设备离线：页面正常显示历史数据，仅提示「上次同步于 X」
6. 空库：显示空态而非报错
7. 断言方式沿用现有做法（Node 假 DOM + 真实接口数据），不截图

---

## 10. 风险与回退

| 风险 | 影响 | 应对 |
|---|---|---|
| 单调时钟被误当 Unix 时间 | 时间轴整体错位 | 解析层强制不读 `timestamp` 作绝对时间，只用本机 `ts` |
| 充电清零丢周期 | 周期列表缺一段 | `.old` 兜底 + 界面标明 `truncated` |
| 采样稀疏 | 日曲线像锯齿 | 主视图以周期为单位；日曲线标注「估算」 |
| 解析规则随 KOReader 升级变化 | 全表 `consistent=0` | 存了 `raw`，可回放重解析 |
| 与统计页争抢设备连接 | 统计页变慢 | 电池采集不挂在 `/api/stats` 上 |

**回退成本极低**：新增 `app/battery.py` 与 `docs/`，对现有文件的改动只有 `server.py` 的路由注册、`index.html` 的一个 section、`app.js` 的三处分支。整块撤销只需删文件 + 回退这三处。

---

## 11. 待主人定夺

1. **侧栏位置与名称**：建议「电池」，排在阅读记录之后、书库之前；也可叫「续航」。
2. **是否要 P2 的日净耗电**：数据稀疏时它给的是近似值，可能不如干脆不画。
3. **是否设每日自动采样**（Windows 计划任务定时请求 `sample`）：能把周期边界收得更准，但要在系统里加一项计划任务，需主人点头；且设备休眠时仍会失败。
4. **是否现在开做 P0**。
