# KWP UI

Kindle Paperwhite 3 的本地管理面板：阅读统计可视化、书库管理、设备状态。
后端是 FastAPI，前端是原生 JS + ECharts，最终以独立窗口运行（pywebview）。

## 依赖

```bash
pip install -r requirements.txt
```

后端复用 `../kwp3_tools/script/kindle_ssh.py`（已内置 KOReader dropbear 的
主机密钥与空密码适配），不复制 SSH 逻辑。若目录结构不同，用环境变量
`KWP3_SCRIPT_DIR` 指向那个 `script` 目录。

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

## 三个页面

| 页面 | 数据来源 | 说明 |
|---|---|---|
| 阅读统计 | `koreader/settings/statistics.sqlite3` | 首次打开会 SFTP 拉一份到 `data/cache/`，之后 90 秒内复用缓存 |
| 书库 | `/mnt/us` 文件树 | 浏览、上传（支持拖拽）、下载、删除、新建文件夹 |
| 设备 | SSH 读 `/proc`、`/sys` | 电量、存储、内存、固件、KOReader 版本、时钟偏差 |

## 接口

启动后访问 `/api/docs` 看 OpenAPI 文档。

## 结构

```
app/config.py    本地配置持久化（设备地址、端口）
app/device.py    SSH/SFTP 封装、设备状态采集、局域网 IP 扫描
app/stats.py     统计库解析 → 前端可直接绘图的结构
app/server.py    REST 接口与静态资源
web/             前端（index.html / app.js / style.css / vendor/echarts.min.js）
run.py           启动入口
```

## 注意事项

- 所有路径限定在 `/mnt/us` 下，删除操作有二次确认，受保护目录直接拒绝
- 电量数据来自 `/sys/class/power_supply/max77696-battery/`，别的机型可能不同
- 3.0.35 内核没有 `MemAvailable`，可用内存回退为 `MemFree`
