/* KWP UI —— 前端逻辑（原生 JS，无构建步骤） */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  view: "stats",
  host: "",
  port: 2222,
  path: "/mnt/us/documents",
  stats: null,
  status: null,
  online: null,
  charts: {},
  loaded: { stats: false, library: false, device: false },
};

/* ---------- 基础工具 ---------- */

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    data = null;
  }
  if (!res.ok) {
    const msg = (data && data.detail) || `请求失败（${res.status}）`;
    const error = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    error.deviceError = !!(data && data.device_error);
    error.status = res.status;
    throw error;
  }
  return data;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .2s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 220);
  }, 3200);
}

function fmtDuration(seconds) {
  if (!seconds) return "0 分钟";
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} 分钟`;
  const h = m / 60;
  return `${h >= 10 ? Math.round(h) : h.toFixed(1)} 小时`;
}

function fmtSize(bytes) {
  if (bytes == null) return "—";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function fmtDate(ts, withTime = false) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  const base = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return withTime ? `${base} ${pad(d.getHours())}:${pad(d.getMinutes())}` : base;
}

/* ---------- 主题色（图表跟随系统深浅色） ---------- */

function palette() {
  const css = getComputedStyle(document.documentElement);
  const read = (name) => css.getPropertyValue(name).trim();
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  return {
    accent: read("--accent"),
    text: read("--text"),
    muted: read("--muted"),
    border: read("--border"),
    panel: read("--panel"),
    ramp: dark
      ? ["#1e332e", "#2b5f52", "#3d8f76", "#4fbfa0"]
      : ["#eaf4f0", "#a9d8c8", "#5fb79c", "#1f7a63"],
  };
}

function chartOf(id, option) {
  let instance = state.charts[id];
  if (!instance) {
    instance = echarts.init(document.getElementById(id), null, { renderer: "canvas" });
    state.charts[id] = instance;
  }
  instance.setOption(option, true);
  return instance;
}

function disposeCharts() {
  Object.values(state.charts).forEach((c) => c.dispose());
  state.charts = {};
  if (state.loaded.stats && state.stats) paintStats();
}

window.addEventListener("resize", () => {
  Object.values(state.charts).forEach((c) => c.resize());
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", disposeCharts);

/* ---------- 视图切换 ---------- */

const VIEW_META = {
  stats: { title: "阅读统计", sub: "来自设备上的 KOReader 阅读记录" },
  library: { title: "书库", sub: "/mnt/us/documents" },
  device: { title: "设备", sub: "连接状态与系统信息" },
};

function switchView(view) {
  state.view = view;
  $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${view}`));
  $("#pageTitle").textContent = VIEW_META[view].title;
  $("#pageSub").textContent = VIEW_META[view].sub;
  paintActions();

  if (view === "stats") {
    if (!state.loaded.stats) loadStats();
    else setTimeout(() => Object.values(state.charts).forEach((c) => c.resize()), 30);
  }
  if (view === "library" && !state.loaded.library) loadFiles(state.path);
  if (view === "device") loadStatus();
}

function paintActions() {
  const box = $("#pageActions");
  box.innerHTML = "";
  if (state.view === "stats") {
    box.appendChild(button("刷新数据", "btn btn-primary", () => loadStats(true)));
  } else if (state.view === "library") {
    box.appendChild(button("刷新", "btn", () => loadFiles(state.path)));
  } else if (state.view === "device") {
    box.appendChild(button("重新读取", "btn btn-primary", () => loadStatus(true)));
  }
}

function button(label, cls, onclick) {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = label;
  b.onclick = onclick;
  return b;
}

/* ---------- 设备连接状态 ---------- */

async function pingDevice() {
  try {
    const res = await api("/api/device/ping");
    setOnline(res.online, res.host);
    return res.online;
  } catch (err) {
    setOnline(false, state.host);
    return false;
  }
}

function setOnline(online, host) {
  state.online = online;
  if (host) state.host = host;
  const dot = $("#deviceDot");
  dot.className = "dot " + (online ? "online" : "offline");
  $("#chipHost").textContent = host || "未配置";
  $("#chipSub").textContent = online ? "已连接" : "未连接";
  const banner = $("#offlineBanner");
  if (online) {
    banner.hidden = true;
  } else {
    banner.hidden = false;
    $("#offlineText").textContent = `连不上 ${host || "设备"}，可能已休眠、SSH 关闭或 IP 变了`;
  }
}

/* ---------- 阅读统计 ---------- */

async function loadStats(force = false) {
  $("#pageSub").textContent = "正在从设备读取统计库…";
  try {
    const data = await api(`/api/stats${force ? "?refresh=1" : ""}`);
    state.stats = data;
    state.loaded.stats = true;
    setOnline(true, data.host);
    paintStats();
    $("#pageSub").textContent = `数据更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
  } catch (err) {
    $("#pageSub").textContent = "读取失败";
    state.loaded.stats = true;
    if (err.deviceError || err.status === 503) {
      setOnline(false, state.host);
      $("#offlineBanner").hidden = false;
      $("#offlineText").textContent = err.message;
    }
    toast(err.message, "err");
  }
}

function paintStats() {
  const d = state.stats;
  if (!d) return;
  const o = d.overview;

  $("#kpiTotal").textContent = fmtDuration(o.total_seconds);
  $("#kpiTotalFoot").textContent = `共 ${o.page_records} 次翻页`;

  $("#kpiWeek").textContent = fmtDuration(o.this_week_seconds);
  $("#kpiWeekFoot").textContent = `日均 ${o.avg_minutes} 分钟`;

  $("#kpiStreak").textContent = `${o.current_streak} 天`;
  $("#kpiStreakFoot").textContent = `最长连续 ${o.longest_streak} 天`;

  $("#kpiBooks").textContent = `${o.total_books} 本`;
  $("#kpiBooksFoot").textContent = o.last_read ? `最近 ${fmtDate(o.last_read)}` : "尚无记录";

  $("#calHint").textContent = `${o.active_days} 天有阅读记录`;
  $("#booksHint").textContent = `${d.books.length} 本`;
  paintCalendar(d.calendar);
  paintDaily(d.daily);
  paintHourly(d.hourly);
  paintBooks(d.books);
}

function isoOf(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function paintCalendar(rows) {
  const p = palette();
  const map = new Map(rows.map((r) => [r[0], r[1]]));
  const dayMs = 86400000;
  const last = new Date(`${rows[rows.length - 1][0]}T00:00:00`);
  const first = new Date(`${rows[0][0]}T00:00:00`);

  // 列 = 周（周一起），行 = 星期，做成连续热力图而不是按月分块
  const start = new Date(first);
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
  const weeks = Math.ceil((Math.round((last - start) / dayMs) + 1) / 7);

  const data = [];
  const monthLabels = [];
  let prevLabelAt = -99;
  for (let w = 0; w < weeks; w += 1) {
    const monday = new Date(start.getTime() + w * 7 * dayMs);
    for (let day = 0; day < 7; day += 1) {
      const d = new Date(start.getTime() + (w * 7 + day) * dayMs);
      if (d > last || d < first) continue;
      const iso = isoOf(d);
      const v = map.get(iso) || 0;
      data.push({ value: [w, day, v], date: iso });
      // 只标季度首月，避免窄屏下月份标签互相压字
      if (d.getDate() === 1 && (d.getMonth() + 1) % 3 === 1 && w - prevLabelAt > 2) {
        monthLabels[w] = `${d.getMonth() + 1}月`;
        prevLabelAt = w;
      }
    }
  }
  const max = Math.max(10, ...data.map((d) => d.value[2]));

  chartOf("calChart", {
    backgroundColor: "transparent",
    grid: { left: 34, right: 10, top: 26, bottom: 18 },
    tooltip: {
      formatter: (params) => `${params.data.date}<br/>阅读 <b>${params.data.value[2]}</b> 分钟`,
      backgroundColor: p.panel,
      borderColor: p.border,
      textStyle: { color: p.text, fontSize: 12 },
    },
    visualMap: {
      show: false,
      min: 0,
      max,
      inRange: { color: p.ramp },
    },
    xAxis: {
      type: "category",
      data: Array.from({ length: weeks }, (_, i) => i),
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: {
        color: p.muted,
        fontSize: 11,
        interval: 0,
        formatter: (value) => monthLabels[value] || "",
      },
    },
    yAxis: {
      type: "category",
      data: ["一", "", "三", "", "五", "", "日"],
      inverse: true,
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { color: p.muted, fontSize: 11 },
    },
    series: [
      {
        type: "heatmap",
        data,
        itemStyle: { borderRadius: 2, borderWidth: 1, borderColor: p.panel },
        emphasis: { itemStyle: { borderColor: p.text, borderWidth: 1 } },
        progressive: 0,
      },
    ],
  });
}

function paintDaily(rows) {
  const p = palette();
  const slice = rows.slice(-30);
  chartOf("dailyChart", {
    backgroundColor: "transparent",
    grid: { left: 40, right: 12, top: 18, bottom: 28 },
    tooltip: {
      trigger: "axis",
      formatter: (ps) => `${ps[0].axisValue}<br/>${ps[0].data} 分钟`,
      backgroundColor: p.panel,
      borderColor: p.border,
      textStyle: { color: p.text, fontSize: 12 },
    },
    xAxis: {
      type: "category",
      data: slice.map((r) => r[0].slice(5)),
      axisLine: { lineStyle: { color: p.border } },
      axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 11, interval: 4 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: p.border } },
      axisLabel: { color: p.muted, fontSize: 11 },
    },
    series: [
      {
        type: "bar",
        data: slice.map((r) => r[1]),
        barMaxWidth: 14,
        itemStyle: { color: p.accent, borderRadius: [3, 3, 0, 0] },
      },
    ],
  });
}

function paintHourly(values) {
  const p = palette();
  const peak = Math.max(...values);
  chartOf("hourChart", {
    backgroundColor: "transparent",
    grid: { left: 40, right: 12, top: 18, bottom: 28 },
    tooltip: {
      trigger: "axis",
      formatter: (ps) => `${ps[0].axisValue} 点<br/>${ps[0].data} 分钟`,
      backgroundColor: p.panel,
      borderColor: p.border,
      textStyle: { color: p.text, fontSize: 12 },
    },
    xAxis: {
      type: "category",
      data: values.map((_, i) => String(i).padStart(2, "0")),
      axisLine: { lineStyle: { color: p.border } },
      axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 11, interval: 3 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: p.border } },
      axisLabel: { color: p.muted, fontSize: 11 },
    },
    series: [
      {
        type: "bar",
        barMaxWidth: 12,
        data: values.map((v) => ({
          value: v,
          itemStyle: { color: v === peak ? p.accent : p.ramp[1], borderRadius: [3, 3, 0, 0] },
        })),
      },
    ],
  });
}

function paintBooks(books) {
  const body = $("#booksTable").querySelector("tbody");
  body.innerHTML = "";
  if (!books.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">还没有阅读记录</td></tr>';
    return;
  }
  books.forEach((b, i) => {
    const tr = document.createElement("tr");
    const pct = b.progress == null ? null : b.progress;
    tr.innerHTML = `
      <td class="col-rank">${i + 1}</td>
      <td><div class="book-title"><b>${escapeHtml(b.title)}</b>${
        b.authors ? `<span>${escapeHtml(b.authors)}</span>` : ""
      }</div></td>
      <td class="col-num">${fmtDuration(b.seconds)}</td>
      <td class="col-progress">${
        pct == null
          ? '<span style="color:var(--muted);font-size:12px">—</span>'
          : `<div class="progress"><div class="bar-bg"><div class="bar-fg" style="width:${pct}%"></div></div><em>${pct}%</em></div>`
      }</td>
      <td class="col-date">${fmtDate(b.last_read)}</td>`;
    body.appendChild(tr);
  });
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------- 书库 ---------- */

async function loadFiles(path) {
  $("#filesBody").innerHTML = '<tr><td colspan="4" class="empty">读取中…</td></tr>';
  try {
    const res = await api(`/api/files?path=${encodeURIComponent(path)}`);
    state.path = res.path;
    state.loaded.library = true;
    setOnline(true, state.host);
    paintCrumbs(res.path, res.parent);
    paintFiles(res.entries);
    $("#pageSub").textContent = res.path;
  } catch (err) {
    $("#filesBody").innerHTML = "";
    $("#filesEmpty").hidden = false;
    $("#filesEmpty").textContent = err.message;
    if (err.deviceError) setOnline(false, state.host);
    toast(err.message, "err");
  }
}

function paintCrumbs(path, parent) {
  const box = $("#crumbs");
  box.innerHTML = "";
  if (parent) {
    const up = document.createElement("button");
    up.textContent = "↑ 上一级";
    up.onclick = () => loadFiles(parent);
    box.appendChild(up);
    const sep = document.createElement("span");
    sep.textContent = "/";
    box.appendChild(sep);
  }
  const parts = path.replace("/mnt/us", "").split("/").filter(Boolean);
  const root = document.createElement("button");
  root.textContent = "/mnt/us";
  root.onclick = () => loadFiles("/mnt/us");
  box.appendChild(root);
  let acc = "/mnt/us";
  parts.forEach((part) => {
    acc += `/${part}`;
    const sep = document.createElement("span");
    sep.textContent = "/";
    box.appendChild(sep);
    const b = document.createElement("button");
    b.textContent = part;
    const target = acc;
    b.onclick = () => loadFiles(target);
    box.appendChild(b);
  });
}

const ICON_DIR = '<svg class="icon dir" viewBox="0 0 16 16"><path d="M1.5 4h4l1.4 1.8h7.6V13H1.5z"/></svg>';
const ICON_FILE = '<svg class="icon" viewBox="0 0 16 16"><path d="M4 1.5h5l3 3v10H4z"/><path d="M9 1.5v3h3"/></svg>';

function paintFiles(entries) {
  const body = $("#filesBody");
  body.innerHTML = "";
  $("#filesEmpty").hidden = entries.length > 0;
  $("#filesEmpty").textContent = "这个文件夹是空的";

  entries.forEach((e) => {
    const tr = document.createElement("tr");

    const nameTd = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "file-name";
    wrap.innerHTML = e.is_dir ? ICON_DIR : ICON_FILE;
    if (e.is_dir) {
      const sp = document.createElement("span");
      sp.textContent = e.name;
      wrap.appendChild(sp);
    } else {
      const b = document.createElement("button");
      b.textContent = e.name;
      b.title = "点击下载";
      b.onclick = () => downloadFile(`${state.path}/${e.name}`);
      wrap.appendChild(b);
    }
    nameTd.appendChild(wrap);
    tr.appendChild(nameTd);

    const sizeTd = document.createElement("td");
    sizeTd.className = "col-size";
    sizeTd.textContent = e.is_dir ? "—" : fmtSize(e.size);
    tr.appendChild(sizeTd);

    const dateTd = document.createElement("td");
    dateTd.className = "col-date";
    dateTd.textContent = fmtDate(e.mtime, true);
    tr.appendChild(dateTd);

    const opsTd = document.createElement("td");
    opsTd.className = "col-ops";
    if (e.is_dir) {
      const open = button("打开", "btn btn-mini", () => loadFiles(`${state.path}/${e.name}`));
      opsTd.appendChild(open);
    } else {
      opsTd.appendChild(button("下载", "btn btn-mini", () => downloadFile(`${state.path}/${e.name}`)));
    }
    const del = button("删除", "btn btn-mini btn-danger", () => removePath(`${state.path}/${e.name}`, e.name));
    del.style.marginLeft = "6px";
    opsTd.appendChild(del);
    tr.appendChild(opsTd);

    body.appendChild(tr);
  });
}

function downloadFile(remote) {
  const name = remote.split("/").pop();
  toast(`开始下载 ${name}`, "ok");
  const a = document.createElement("a");
  a.href = `/api/files/download?path=${encodeURIComponent(remote)}`;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function removePath(remote, label) {
  const ok = await confirmDialog("确认删除？", `将永久删除「${label}」，无法恢复。`);
  if (!ok) return;
  try {
    const res = await api("/api/files/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: [remote] }),
    });
    if (res.failed.length) {
      toast(res.failed[0].error, "err");
    } else {
      toast(`已删除 ${label}`, "ok");
    }
    loadFiles(state.path);
  } catch (err) {
    toast(err.message, "err");
  }
}

async function uploadFiles(fileList) {
  if (!fileList || !fileList.length) return;
  const form = new FormData();
  form.append("path", state.path);
  Array.from(fileList).forEach((f) => form.append("files", f));
  toast(`正在上传 ${fileList.length} 个文件…`);
  try {
    const res = await api("/api/files/upload", { method: "POST", body: form });
    if (res.saved.length) toast(`已上传 ${res.saved.length} 个文件`, "ok");
    res.failed.forEach((f) => toast(`${f.name}：${f.error}`, "err"));
    loadFiles(state.path);
  } catch (err) {
    toast(err.message, "err");
  }
}

async function newFolder() {
  const name = await promptDialog("新建文件夹", "文件夹名称", "");
  if (!name) return;
  try {
    await api("/api/files/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.path, name }),
    });
    toast("已创建", "ok");
    loadFiles(state.path);
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ---------- 设备 ---------- */

async function loadStatus(force = false) {
  if (state.status && !force) {
    paintStatus(state.status);
    return;
  }
  $("#pageSub").textContent = "正在读取设备状态…";
  try {
    const s = await api("/api/device/status");
    state.status = s;
    setOnline(true, s.host);
    paintStatus(s);
    $("#pageSub").textContent = `已连接 ${s.host}`;
  } catch (err) {
    $("#pageSub").textContent = "设备未连接";
    setOnline(false, state.host);
    $("#offlineBanner").hidden = false;
    $("#offlineText").textContent = err.message;
    toast(err.message, "err");
  }
}

function paintStatus(s) {
  const pct = s.battery == null ? 0 : s.battery;
  $("#battRing").style.setProperty("--pct", pct);
  $("#battRing").innerHTML = `<span>${s.battery == null ? "—" : s.battery + "%"}</span>`;
  $("#battState").textContent = s.charging || "—";
  $("#battLabel").textContent = s.battery == null ? "未知" : `${s.battery}% 电量`;
  const charging = (s.charging || "").toLowerCase();
  $("#battHint").textContent =
    charging === "charging" ? "正在充电" : charging === "discharging" ? "电池供电中" : s.charging || "";

  const st = s.storage;
  if (st) {
    $("#storeBar").style.width = `${st.percent}%`;
    $("#storeFree").textContent = `${(st.free_mb / 1024).toFixed(1)} GB`;
    $("#storeUsed").textContent = `${(st.used_mb / 1024).toFixed(1)} GB`;
    $("#storeTotal").textContent = `${(st.total_mb / 1024).toFixed(1)} GB`;
  }

  $("#latencyHint").textContent = `响应 ${s.latency_ms} ms`;

  const hours = Math.floor(s.uptime_sec / 3600);
  const days = Math.floor(hours / 24);
  const rows = [
    ["型号", s.model || "—"],
    ["序列号", s.serial || "—"],
    ["系统固件", s.firmware || "—"],
    ["内核", s.kernel || "—"],
    ["KOReader", s.koreader || "—"],
    ["运行时长", days > 0 ? `${days} 天 ${hours % 24} 小时` : `${hours} 小时`],
    ["可用内存", `${s.memory.available_mb} / ${s.memory.total_mb} MB`],
    ["时钟偏差", s.clock_skew_sec == null ? "—" : `${s.clock_skew_sec} 秒`],
    ["地址", `${s.host}:${s.port}`],
  ];
  const dl = $("#deviceInfo");
  dl.innerHTML = rows
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${escapeHtml(String(v))}</dd></div>`)
    .join("");
}

async function saveHost() {
  const host = $("#hostInput").value.trim();
  const port = parseInt($("#portInput").value.trim() || "2222", 10);
  if (!host) {
    toast("请填写设备地址", "err");
    return;
  }
  try {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, port }),
    });
    state.host = host;
    state.status = null;
    const online = await pingDevice();
    toast(online ? `已连接 ${host}` : `连不上 ${host}`, online ? "ok" : "err");
    if (online) loadStatus(true);
  } catch (err) {
    toast(err.message, "err");
  }
}

async function scanDevices() {
  const box = $("#scanResult");
  box.innerHTML = '<div class="scan-item"><span>正在扫描局域网 2222 端口…</span></div>';
  try {
    const res = await api("/api/device/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    box.innerHTML = "";
    if (!res.found.length) {
      box.innerHTML = `<div class="scan-item"><span>${res.subnet}.0/24 网段没找到开启 SSH 的设备</span></div>`;
      return;
    }
    res.found.forEach((f) => {
      const row = document.createElement("div");
      row.className = "scan-item";
      row.innerHTML = `<div><code>${f.host}</code><br><small>${escapeHtml(f.software || f.banner)}</small></div>`;
      const use = button("使用此设备", "btn btn-mini btn-primary", async () => {
        $("#hostInput").value = f.host;
        await saveHost();
        loadStatus(true);
      });
      row.appendChild(use);
      box.appendChild(row);
    });
  } catch (err) {
    box.innerHTML = "";
    toast(err.message, "err");
  }
}

/* ---------- 简易对话框 ---------- */

function dialogShell(html) {
  const mask = document.createElement("div");
  mask.className = "overlay";
  mask.innerHTML = `<div class="modal">${html}</div>`;
  document.body.appendChild(mask);
  return mask;
}

function promptDialog(title, label, value = "") {
  return new Promise((resolve) => {
    const mask = dialogShell(`
      <h3>${escapeHtml(title)}</h3>
      <label class="field"><span>${escapeHtml(label)}</span><input id="dlgInput" value="${escapeHtml(value)}"></label>
      <div class="modal-actions"><button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="ok">确定</button></div>`);
    const input = mask.querySelector("#dlgInput");
    input.focus();
    input.select();
    const done = (val) => {
      mask.remove();
      resolve(val);
    };
    mask.querySelector('[data-act="cancel"]').onclick = () => done(null);
    mask.querySelector('[data-act="ok"]').onclick = () => done(input.value.trim() || null);
    input.onkeydown = (e) => {
      if (e.key === "Enter") done(input.value.trim() || null);
      if (e.key === "Escape") done(null);
    };
  });
}

function confirmDialog(title, text) {
  return new Promise((resolve) => {
    const mask = dialogShell(`
      <h3>${escapeHtml(title)}</h3>
      <p class="modal-text">${escapeHtml(text)}</p>
      <div class="modal-actions"><button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="ok">确认</button></div>`);
    const done = (val) => {
      mask.remove();
      resolve(val);
    };
    mask.querySelector('[data-act="cancel"]').onclick = () => done(false);
    mask.querySelector('[data-act="ok"]').onclick = () => done(true);
  });
}

/* ---------- 事件绑定 ---------- */

function bindEvents() {
  $$(".nav-item").forEach((b) => b.onclick = () => switchView(b.dataset.view));
  $("#deviceChip").onclick = () => switchView("device");
  $("#bannerScan").onclick = () => {
    switchView("device");
    scanDevices();
  };
  $("#btnUpload").onclick = () => $("#fileInput").click();
  $("#fileInput").onchange = (e) => {
    uploadFiles(e.target.files);
    e.target.value = "";
  };
  $("#btnMkdir").onclick = newFolder;
  $("#btnSaveHost").onclick = saveHost;
  $("#btnScan").onclick = scanDevices;

  const mask = $("#dropMask");
  let depth = 0;
  window.addEventListener("dragenter", (e) => {
    if (state.view !== "library") return;
    e.preventDefault();
    depth += 1;
    mask.classList.add("on");
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("dragleave", (e) => {
    if (state.view !== "library") return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) mask.classList.remove("on");
  });
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0;
    mask.classList.remove("on");
    if (state.view !== "library") return;
    uploadFiles(e.dataTransfer.files);
  });
}

/* ---------- 启动 ---------- */

async function boot() {
  bindEvents();
  try {
    const cfg = await api("/api/config");
    state.host = cfg.host;
    state.port = cfg.port;
    state.path = cfg.last_library_path || "/mnt/us/documents";
    $("#hostInput").value = cfg.host;
    $("#portInput").value = cfg.port;
    $("#chipHost").textContent = cfg.host;
  } catch (err) {
    toast("读取本地配置失败", "err");
  }
  pillStatusStarting();
  await pingDevice();
  switchView("stats");
}

function pillStatusStarting() {
  $("#deviceDot").className = "dot";
  $("#chipSub").textContent = "检测中…";
}

boot();
