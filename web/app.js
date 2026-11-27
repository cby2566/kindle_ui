/* KWP UI —— 前端逻辑（原生 JS，无构建步骤） */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const WEEK_NAMES = ["一", "二", "三", "四", "五", "六", "日"];
const WEEK_FULL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const state = {
  view: "stats",
  host: "",
  port: 2222,
  path: "/mnt/us/documents",
  range: "all",
  calMonth: null,          // {year, month}
  selectedDate: null,      // 'YYYY-MM-DD'
  stats: null,
  calendar: null,
  day: null,
  status: null,
  online: null,
  cacheOffline: false,
  cachedAt: null,
  charts: {},
  timeline: null,          // vis.Timeline 实例
  colors: new Map(),       // book_id -> 调色板序号 1..8
  colorSeq: 0,
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

function fmtClock(seconds) {
  const s = Math.max(0, Math.min(86400, Math.round(seconds)));
  const h = Math.floor(s / 3600) % 24;
  const m = Math.floor((s % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function fmtDateTime(ts) {
  if (!ts) return "未知时间";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function isoOf(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function parseDate(iso) {
  return new Date(`${iso}T00:00:00`);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* 书的本色：同一本书在日历、时间轴、排行榜里颜色一致 */
function bookColorIndex(id) {
  if (!state.colors.has(id)) {
    state.colorSeq += 1;
    state.colors.set(id, ((state.colorSeq - 1) % 8) + 1);
  }
  return state.colors.get(id);
}

function bookColor(id) {
  return `var(--book-${bookColorIndex(id)})`;
}

/* ---------- 主题与图表 ---------- */

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
    soft: dark ? "#2a3f39" : "#a9d8c8",
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
  if (state.timeline) {
    try {
      state.timeline.redraw();
    } catch (err) {
      /* 已销毁 */
    }
  }
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
    else setTimeout(() => {
      Object.values(state.charts).forEach((c) => c.resize());
      // 视图从 display:none 切回来后，vis 需要重新量一次尺寸
      if (state.timeline) {
        try {
          state.timeline.redraw();
        } catch (err) {
          /* 已销毁 */
        }
      }
    }, 30);
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
  $("#deviceDot").className = "dot " + (online ? "online" : "offline");
  $("#chipHost").textContent = host || "未配置";
  $("#chipSub").textContent = online
    ? "已连接"
    : state.cacheOffline ? "离线 · 用缓存" : "未连接";
  updateBanner();
}

function updateBanner() {
  const banner = $("#offlineBanner");
  if (state.cacheOffline) {
    banner.hidden = false;
    banner.classList.add("warn");
    $("#offlineText").textContent =
      `连不上设备，正在看 ${fmtDateTime(state.cachedAt)} 缓存的统计数据`;
    return;
  }
  banner.classList.remove("warn");
  if (state.online === false) {
    banner.hidden = false;
    $("#offlineText").textContent = `连不上 ${state.host || "设备"}，可能已休眠、SSH 关闭或 IP 变了`;
    return;
  }
  banner.hidden = true;
}

/* ---------- 阅读统计：概览 ---------- */

async function loadStats(force = false) {
  $("#pageSub").textContent = "正在从设备读取统计库…";
  try {
    const data = await api(`/api/stats?range=${state.range}${force ? "&refresh=1" : ""}`);
    state.stats = data;
    state.loaded.stats = true;
    state.cachedAt = data.cached_at;
    // 只有本次真的连上设备才算在线；命中有效期内的缓存不改变在线判断，
    // 否则设备刚断开时会被缓存"伪装"成在线
    if (data.source === "device") {
      state.cacheOffline = false;
      setOnline(true, data.host);
    } else if (data.offline) {
      state.cacheOffline = true;
      setOnline(false, data.host);
    } else {
      state.cacheOffline = state.online === false;
      updateBanner();
    }
    paintStats();
    if (state.cacheOffline) {
      $("#pageSub").textContent = `离线模式 · 缓存于 ${fmtDateTime(data.cached_at)}`;
    } else if (data.source === "device") {
      $("#pageSub").textContent = `数据更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
    } else {
      $("#pageSub").textContent = `数据取自 ${fmtDateTime(data.cached_at)} 的本地缓存`;
    }
    if (state.cacheOffline && force) {
      toast("设备仍未连接，继续显示缓存数据");
    }
    await Promise.all([loadCalendar(), loadDay(state.selectedDate)]);
  } catch (err) {
    $("#pageSub").textContent = "读取失败";
    state.loaded.stats = true;
    state.cacheOffline = false;
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
  $("#kpiTotalFoot").textContent = `日均 ${o.avg_minutes} 分钟`;

  $("#kpiPages").textContent = o.page_records.toLocaleString("zh-CN");
  $("#kpiPagesFoot").textContent = `涉及 ${o.total_books} 本书`;

  $("#kpiDays").textContent = `${o.active_days} 天`;
  $("#kpiDaysFoot").textContent = `累计最长连续 ${o.longest_streak} 天`;

  $("#kpiSpeed").textContent = o.speed ? `${o.speed}` : "—";
  $("#kpiSpeedFoot").textContent = o.speed ? "页 / 小时" : "数据不足";

  $("#hourHint").textContent = `${d.range_label}累计 / 分钟`;
  $("#booksHint").textContent = `${d.books.length} 本`;
  $("#rangeHint").textContent = d.range_start
    ? `${d.range_start} 至今 · 主数据源 statistics.sqlite3`
    : `有记录以来 · 主数据源 statistics.sqlite3`;

  paintDaily(d.daily);
  paintHourly(d.hourly);
  paintBooks(d.books);
}

function paintDaily(rows) {
  const p = palette();
  const slice = rows.slice(-30);
  chartOf("dailyChart", {
    backgroundColor: "transparent",
    grid: { left: 42, right: 44, top: 20, bottom: 28 },
    tooltip: {
      trigger: "axis",
      formatter: (ps) =>
        `${ps[0].axisValue}<br/>${ps[0].data} 分钟 · ${ps[1] ? ps[1].data : 0} 页`,
      backgroundColor: p.panel,
      borderColor: p.border,
      textStyle: { color: p.text, fontSize: 12 },
    },
    legend: { show: false },
    xAxis: {
      type: "category",
      data: slice.map((r) => r[0].slice(5)),
      axisLine: { lineStyle: { color: p.border } },
      axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 11, interval: 4 },
    },
    yAxis: [
      {
        type: "value",
        splitLine: { lineStyle: { color: p.border } },
        axisLabel: { color: p.muted, fontSize: 11 },
      },
      {
        type: "value",
        splitLine: { show: false },
        axisLabel: { color: p.muted, fontSize: 11 },
      },
    ],
    series: [
      {
        name: "分钟",
        type: "bar",
        data: slice.map((r) => r[1]),
        barMaxWidth: 14,
        itemStyle: { color: p.accent, borderRadius: [3, 3, 0, 0] },
      },
      {
        name: "页数",
        type: "line",
        yAxisIndex: 1,
        data: slice.map((r) => r[2]),
        symbol: "none",
        smooth: true,
        lineStyle: { color: p.soft, width: 1.5 },
      },
    ],
  });
}

function paintHourly(values) {
  const p = palette();
  const peak = Math.max(...values);
  chartOf("hourChart", {
    backgroundColor: "transparent",
    grid: { left: 42, right: 12, top: 20, bottom: 28 },
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
          itemStyle: {
            color: v === peak && v > 0 ? p.accent : p.soft,
            borderRadius: [3, 3, 0, 0],
          },
        })),
      },
    ],
  });
}

function paintBooks(books) {
  const body = $("#booksTable").querySelector("tbody");
  body.innerHTML = "";
  if (!books.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">当前范围没有阅读记录</td></tr>';
    return;
  }
  books.forEach((b, i) => {
    const tr = document.createElement("tr");
    const pct = b.progress;
    tr.innerHTML = `
      <td class="col-rank"><i class="swatch" style="background:${bookColor(b.id)}"></i>${i + 1}</td>
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

/* ---------- 日历视图（手写，不用日历库） ---------- */

async function loadCalendar(year, month) {
  if (!year) {
    const now = new Date();
    if (!state.calMonth) state.calMonth = { year: now.getFullYear(), month: now.getMonth() + 1 };
    year = state.calMonth.year;
    month = state.calMonth.month;
  }
  try {
    const data = await api(`/api/stats/calendar?month=${year}-${String(month).padStart(2, "0")}`);
    state.calendar = data;
    paintCalendar(data);
  } catch (err) {
    toast(err.message, "err");
  }
}

function paintCalendar(cal) {
  $("#calTitle").textContent = `${cal.year} 年 ${cal.month} 月`;
  const totalMin = Math.round(cal.total_seconds / 60);
  const active = cal.days.filter((d) => d.seconds > 0).length;
  $("#calSummary").textContent = totalMin
    ? `本月 ${active} 天有阅读，共 ${fmtDuration(cal.total_seconds)}`
    : "本月还没有阅读记录";

  // 星期表头
  const head = $("#calWeekHead");
  head.innerHTML = "";
  WEEK_NAMES.forEach((n) => {
    const el = document.createElement("span");
    el.textContent = n;
    head.appendChild(el);
  });

  // 日格
  const grid = $("#calGrid");
  grid.innerHTML = "";
  const offset = cal.first_weekday - cal.week_start;
  const blanks = (offset + 7) % 7;
  for (let i = 0; i < blanks; i += 1) {
    const b = document.createElement("div");
    b.className = "cal-blank";
    grid.appendChild(b);
  }

  const todayIso = cal.today;
  cal.days.forEach((d) => {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "cal-day";
    cell.dataset.date = d.date;
    if (d.seconds > 0) cell.classList.add("has-data");
    if (d.date === todayIso) cell.classList.add("is-today");
    if (d.date === state.selectedDate) cell.classList.add("is-sel");

    // 底色深浅按当天总时长（很淡，真正的信息在直方图和色条上）
    if (d.seconds > 0 && cal.max_day_seconds > 0) {
      const ratio = Math.pow(d.seconds / cal.max_day_seconds, 0.6);
      cell.style.setProperty("--tint", (0.05 + ratio * 0.16).toFixed(3));
    }

    const num = document.createElement("span");
    num.className = "cal-num";
    num.textContent = d.day;
    cell.appendChild(num);

    if (d.hidden > 0) {
      const more = document.createElement("span");
      more.className = "cal-more";
      more.textContent = `+${d.hidden}`;
      cell.appendChild(more);
    }

    const histo = document.createElement("div");
    histo.className = "cal-histo";
    d.hours.forEach((minutes) => {
      const bar = document.createElement("i");
      // 与 KOReader 的 HistogramWidget 一致：ratio = 该小时时长/1 小时，
      // 满一小时顶到最高，非零至少 1px，不做跨日归一化
      const h = minutes > 0
        ? Math.max(1, Math.round(Math.min(1, minutes / 60) * 20))
        : 0;
      bar.style.height = `${h}px`;
      if (h > 0) bar.classList.add("on");      histo.appendChild(bar);
    });
    cell.appendChild(histo);

    cell.onclick = () => selectDate(d.date);
    grid.appendChild(cell);
  });

  paintSpans(cal);
  paintLegend(cal);
}

function paintSpans(cal) {
  const layer = $("#calSpans");
  layer.innerHTML = "";
  const offset = cal.first_weekday - cal.week_start;
  const blanks = (offset + 7) % 7;

  cal.spans.forEach((s) => {
    const el = document.createElement("div");
    el.className = "cal-span";
    const col = s.week_col + 1;
    const row = s.week + 1;
    el.style.gridColumn = `${col} / span ${s.days}`;
    el.style.gridRow = `${row}`;
    el.style.marginTop = `calc(21px + ${s.lane} * 14px)`;
    el.style.background = bookColor(s.id);
    el.title = `${s.title} · ${s.days} 天`;
    // 色条浮在日格之上，不转发点击的话点到色条就选不中这一天
    const dayDate = cal.days[s.start_day] ? cal.days[s.start_day].date : null;
    if (dayDate) el.onclick = () => selectDate(dayDate);
    const label = document.createElement("span");
    label.textContent = s.title;
    el.appendChild(label);
    layer.appendChild(el);
  });
}

function paintLegend(cal) {
  const map = new Map();
  cal.spans.forEach((s) => {
    if (!map.has(s.id)) map.set(s.id, s.title);
  });
  const box = $("#calLegend");
  box.innerHTML = "";
  Array.from(map.entries()).slice(0, 8).forEach(([id, title]) => {
    const item = document.createElement("span");
    item.className = "legend-item";
    item.innerHTML = `<i style="background:${bookColor(id)}"></i><span title="${escapeHtml(title)}">${escapeHtml(title)}</span>`;
    box.appendChild(item);
  });
}

async function selectDate(date) {
  state.selectedDate = date;
  $$(".cal-day").forEach((c) => c.classList.toggle("is-sel", c.dataset.date === date));
  await loadDay(date);
}

/* ---------- 时间轴（手写） ---------- */

async function loadDay(date) {
  if (!date) date = state.selectedDate || isoOf(new Date());
  state.selectedDate = date;
  try {
    const data = await api(`/api/stats/day?date=${date}`);
    state.day = data;
    paintTimeline(data);
  } catch (err) {
    toast(err.message, "err");
  }
}

function paintTimeline(day) {
  $("#tlDate").textContent = `${day.date} ${WEEK_FULL[day.weekday - 1]}`;
  $("#tlSummary").textContent = day.seconds
    ? `${fmtDuration(day.seconds)} · ${day.page_records} 次翻页 · ${day.books.length} 本书`
    : "没有阅读记录";

  const wrap = $("#tlWrap");
  if (state.timeline) {
    try {
      state.timeline.destroy();
    } catch (err) {
      /* 容器已移除，忽略 */
    }
    state.timeline = null;
  }
  wrap.innerHTML = "";

  if (!day.books.length) {
    const empty = document.createElement("div");
    empty.className = "tl-empty";
    empty.textContent = "这一天没有阅读记录";
    wrap.appendChild(empty);
    return;
  }

  const dayStart = parseDate(day.date).getTime();
  const dayEnd = dayStart + 86400000;
  const isToday = day.date === isoOf(new Date());

  // 每本书一个分组（行），左侧标签用 DOM，避免库把 HTML 当纯文本
  const groups = new vis.DataSet(day.books.map((b) => {
    const label = document.createElement("div");
    label.className = "vt-group";
    const sw = document.createElement("i");
    sw.className = "swatch";
    sw.style.background = bookColor(b.id);
    const name = document.createElement("span");
    name.textContent = b.title;
    name.title = b.title;
    const mins = document.createElement("em");
    const m = (b.raw_seconds || b.seconds) / 60;
    mins.textContent = m < 1 ? "<1 分" : `${Math.round(m)} 分`;
    label.append(sw, name, mins);
    return { id: b.id, content: label };
  }));

  const items = [];
  day.books.forEach((b) => {
    b.periods.forEach((p) => {
      const minutes = Math.max(1, Math.round((p.finish - p.start) / 60));
      items.push({
        group: b.id,
        start: new Date(dayStart + p.start * 1000),
        end: new Date(dayStart + p.finish * 1000),
        title: `${fmtClock(p.start)} – ${fmtClock(p.finish)} · ${minutes} 分钟`,
        className: `kwp-item bk-${bookColorIndex(b.id)}`,
      });
    });
  });

  const canvas = document.createElement("div");
  canvas.className = "tl-canvas";
  canvas.style.height = `${Math.max(100, day.books.length * 30 + 50)}px`;
  wrap.appendChild(canvas);

  state.timeline = new vis.Timeline(canvas, new vis.DataSet(items), groups, {
    start: new Date(dayStart),
    end: new Date(dayEnd),
    min: new Date(dayStart),
    max: new Date(dayEnd),
    timeAxis: { scale: "hour", step: 3 },
    orientation: { axis: "top" },
    showCurrentTime: isToday,
    stack: false,
    moveable: true,
    zoomable: true,
    zoomMin: 1000 * 60 * 60,
    zoomMax: 1000 * 60 * 60 * 24,
    height: "100%",
    margin: { axis: 10, item: { horizontal: 0, vertical: 3 } },
    tooltip: { followMouse: true, overflowMethod: "cap" },
    format: {
      minorLabels: { hour: "HH:mm", minute: "HH:mm" },
      majorLabels: { hour: "M月D日" },
    },
  });

  const foot = document.createElement("div");
  foot.className = "tl-foot";
  const first = day.books.reduce(
    (min, b) => Math.min(min, b.periods[0] ? b.periods[0].start : Infinity), Infinity);
  const last = day.books.reduce(
    (max, b) => Math.max(max, b.periods.length ? b.periods[b.periods.length - 1].finish : 0), 0);
  foot.innerHTML = `<span>从 <b>${fmtClock(first)}</b> 读到 <b>${fmtClock(last)}</b></span>
    <span>滚轮缩放 · 拖动平移 · 30 秒内的间隙算作连续阅读</span>`;
  wrap.appendChild(foot);
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
      opsTd.appendChild(button("打开", "btn btn-mini", () => loadFiles(`${state.path}/${e.name}`)));
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

  $("#rangeSeg").onclick = (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    state.range = btn.dataset.range;
    $$("#rangeSeg button").forEach((b) => b.classList.toggle("is-on", b === btn));
    loadStats();
  };

  $("#calPrev").onclick = () => shiftMonth(-1);
  $("#calNext").onclick = () => shiftMonth(1);
  $("#calToday").onclick = () => {
    const now = new Date();
    state.calMonth = { year: now.getFullYear(), month: now.getMonth() + 1 };
    loadCalendar();
  };
  $("#tlPrev").onclick = () => shiftDay(-1);
  $("#tlNext").onclick = () => shiftDay(1);
  $("#tlToday").onclick = () => {
    const today = isoOf(new Date());
    state.calMonth = { year: new Date().getFullYear(), month: new Date().getMonth() + 1 };
    loadCalendar();
    selectDate(today);
  };

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

function shiftMonth(delta) {
  const cur = state.calMonth || { year: new Date().getFullYear(), month: new Date().getMonth() + 1 };
  let { year, month } = cur;
  month += delta;
  if (month < 1) {
    month = 12;
    year -= 1;
  } else if (month > 12) {
    month = 1;
    year += 1;
  }
  state.calMonth = { year, month };
  loadCalendar(year, month);
}

function shiftDay(delta) {
  const base = state.selectedDate ? parseDate(state.selectedDate) : new Date();
  base.setDate(base.getDate() + delta);
  const iso = isoOf(base);
  // 跨月时同步日历
  if (!state.calMonth || base.getMonth() + 1 !== state.calMonth.month || base.getFullYear() !== state.calMonth.year) {
    state.calMonth = { year: base.getFullYear(), month: base.getMonth() + 1 };
    loadCalendar(state.calMonth.year, state.calMonth.month);
  }
  selectDate(iso);
}

/* ---------- 启动 ---------- */

async function boot() {
  bindEvents();
  state.selectedDate = isoOf(new Date());
  const now = new Date();
  state.calMonth = { year: now.getFullYear(), month: now.getMonth() + 1 };

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
  $("#deviceDot").className = "dot";
  $("#chipSub").textContent = "检测中…";
  await pingDevice();
  switchView("stats");
}

boot();
