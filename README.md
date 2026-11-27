# KWP UI

Kindle Paperwhite 3 的本地管理面板：阅读统计可视化、书库管理、设备状态。
后端是 FastAPI，前端是原生 JS + ECharts，最终以独立窗口运行（pywebview）。

## 依赖

```bash
pip install -r requirements.txt
```

SSH 逻辑在自己的 `app/kindle_ssh.py` 里（已内置 KOReader dropbear 的主机密钥与
空密码适配），不引用外部目录，克隆下来装完依赖即可运行；要改 SSH 细节只改这一份。

## 运行

```bash
python run.py            # 独立窗口
python run.py --dev      # 浏览器模式，改前端自动生效
python run.py --host 192.168.31.215
```

服务只监听 `127.0.0.1`，局域网内其它机器访问不到。

## 前置条件（设备侧）

1. KOReader 打开 `Tools → SSH server`
2. 勾选 `Login without password`，再启动服务
3. 记住设备当前 IP（DHCP 会变，界面上可扫描局域网重新定位）

## 页面

| 页面 | 数据来源 | 说明 |
|---|---|---|
| 阅读统计 | `koreader/settings/statistics.sqlite3` | 首次打开会 SFTP 拉一份到 `data/cache/`，之后 90 秒内复用缓存；五个子页：概览 / 周 / 日历 / 单日 / 书籍 |
| 洞察 | 本地缓存的统计库 | 跟随时间范围（今天 / 本周 / 本月 / 今年 / 全部）给出小结、近 12 个月趋势、星期 × 时段热力图与几条自动总结的规律 |
| 阅读记录 | 本地缓存的统计库 | 把翻页记录合成「一次阅读」（相邻两页间隔不超过 15 分钟），可按时间段、按书筛选，整份导出 CSV |
| 书库 | `/mnt/us` 文件树 | 浏览、上传（支持拖拽）、下载、删除、新建文件夹；本地模式下入口隐藏，由洞察与阅读记录补位 |
| 设备 | SSH 读 `/proc`、`/sys` | 电量、存储、内存、固件、KOReader 版本、时钟偏差 |

### 离线可用

阅读统计不依赖实时连接：设备未连上（关机、KOReader 关闭、IP 变了）时，
自动退回到 `data/cache/statistics.sqlite3`，照常看历史数据，顶部显示琥珀色
「离线模式 · 缓存于 X」提示。缓存文件跨重启保留。

洞察与阅读记录两页只读这份缓存，同样离线可用；它们的「刷新数据」只重读缓存，
要从设备拉新库，去阅读统计的概览或书籍页点刷新。

注意：如果**从未成功连接过**，本地没有缓存，此时读统计会返回 503 并提示先连一次设备。
书库和设备两个页面本质上需要实时访问，离线时不可用。

拉库是**原子**的：先下到 `statistics.sqlite3.part`，校验（非空、`PRAGMA quick_check`
通过、含 `page_stat_data` 表）之后才 `os.replace` 换上去，并且整段拉库用一把进程内
锁串行化。所以刷新途中去翻别的页不会读到写了一半的库（以前会 500 或显示「累计 0 分钟」），
拉库失败也不会毁掉原缓存 —— 旧库原地不动，只记一条 `cache_error`。

离线判断加了 TCP 探活（3 秒超时），否则 Windows 的 SYN 重试要 21 秒才报错，
界面会长时间转圈。

## 接口

启动后访问 `/api/docs` 看 OpenAPI 文档。

## 结构

```
app/config.py    本地配置持久化（设备地址、端口）
app/device.py    SSH/SFTP 封装、设备状态采集、局域网 IP 扫描
app/stats.py     统计库解析 → 前端可直接绘图的结构
app/server.py    REST 接口与静态资源
web/             前端（index.html / app.js / style.css）
web/vendor/      离线内置的第三方库：echarts 5.5.1、vis-timeline 7.7.3
run.py           启动入口
```

## 前端依赖

只有两个，都放在 `web/vendor/` 里本地加载，不依赖 CDN：

| 库 | 版本 | 许可 | 用途 |
|---|---|---|---|
| ECharts | 5.5.1 | Apache-2.0 | 每日时长、时段偏好的柱状图 |
| vis-timeline | 7.7.3 | Apache-2.0 / MIT 双许可 | 单日时间轴（缩放、平移、时间刻度） |

日历视图**没有用任何日历库**，是用 CSS Grid 手写的：月历网格 + 跨天色条用同模板的
覆盖层定位（`grid-column: N / span 天数`）+ 每格内嵌 24 小时直方图。跨天合并算法
对齐 KOReader 的 `CalendarWeek:addDay`（`span_days`）。

## 注意事项

- 所有路径限定在 `/mnt/us` 下，删除操作有二次确认，受保护目录直接拒绝
- 窗口模式支持下载：pywebview 的 `ALLOW_DOWNLOADS` 默认关着、会把下载静默取消，
  `run.py` 里已显式打开。导出 CSV 由后端写进 `data/exports/` 并在提示里回报文件名；
  书库下载会弹系统保存框
- 电量数据来自 `/sys/class/power_supply/max77696-battery/`，别的机型可能不同
- 3.0.35 内核没有 `MemAvailable`，可用内存按 `MemFree + Buffers + Cached` 估算
