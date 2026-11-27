/* KWP UI —— 前端逻辑（原生 JS，无构建步骤） */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const WEEK_NAMES = ["一", "二", "三", "四", "五", "六", "日"];
const WEEK_FULL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const state = {
  view: "stats",
  sub: "overview",         // 统计页的子页：overview / week / calendar / day / books
  host: "",
  port: 2222,
  path: "/mnt/us/documents",
  range: "week",           // 默认看本周；概览与洞察共用
  calMonth: null,          // {year, month}
  selectedDate: null,      // 'YYYY-MM-DD'
  weekStart: null,         // 'YYYY-MM-DD'，该周周一
  week: null,
  stats: null,
  calendar: null,
  day: null,
  status: null,
  online: null,
  cacheOffline: false,
  // 本地模式：只读缓存，彻底不碰设备。跨会话记住，重开还是这个模式
  local: localStorage.getItem("kwp.localMode") === "1",
  cachedAt: null,
  charts: {},
  timeline: null,          // vis.Timeline 实例
  colors: new Map(),       // book_id -> 调色板序号 1..8
  colorSeq: 0,
  // 洞察 / 阅读记录两页的数据与筛选状态
  insights: null,
  sessions: null,
  sessionRows: [],
  sessionShown: 0,
  sessionPeriod: "7d",
  sessionBook: "",
  loaded: { stats: false, library: false, device: false },
};

const SUB_META = {
  overview: { title: "概览", sub: "区间合计、每日趋势与时段偏好" },
  week: { title: "周视图", sub: "这一周与上一周的对照" },
  calendar: { title: "日历", sub: "按月铺开，看哪几天读了书" },
  day: { title: "单日", sub: "一天之内的阅读时间轴" },
  books: { title: "书籍", sub: "书目排行与阅读进度" },
};
const SUBS = Object.keys(SUB_META);

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
    // 热力图里「没读过」的格子：比 soft 更淡，压在底色上像一张空格子纸
    heatLow: dark ? "#1e332e" : "#eef4f1",
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
  if (state.view === "insights" && state.insights) paintInsights(state.insights);
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

/* ---------- 视图切换与路由 ---------- */

const VIEW_META = {
  stats: { title: "阅读统计", sub: "来自设备上的 KOReader 阅读记录" },
  insights: { title: "洞察", sub: "规律与节奏" },
  records: { title: "阅读记录", sub: "按「一次阅读」列出" },
  library: { title: "书库", sub: "/mnt/us/documents" },
  device: { title: "设备", sub: "连接状态与系统信息" },
};
/* 没有子页、地址栏只写 #名字 的视图 */
const SIMPLE_VIEWS = ["library", "device", "insights", "records"];

function buildHash(view, sub, arg) {
  if (view !== "stats") return `#${view}`;
  return "#" + ["stats", sub, arg].filter(Boolean).join("/");
}

function parseHash() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  if (!raw) return { view: "stats", sub: "overview", arg: null };
  const parts = raw.split("/").filter(Boolean);
  const view = SIMPLE_VIEWS.includes(parts[0]) ? parts[0] : "stats";
  if (view !== "stats") return { view, sub: null, arg: null };
  const sub = SUBS.includes(parts[1]) ? parts[1] : "overview";
  return { view: "stats", sub, arg: parts[2] || null };
}

/** 唯一的路由入口：落地状态、刷新标题、同步地址栏。 */
function go(view, sub, arg) {
  // 本地模式下书库不可用：这块位置由洞察顶上
  if (view === "library" && state.local) view = "insights";
  applyView(view);
  if (view === "stats") applySub(sub || "overview", arg || null);
  const hash = buildHash(view, view === "stats" ? (sub || "overview") : null,
                         view === "stats" ? arg : null);
  if (location.hash !== hash) {
    // replaceState 不触发 hashchange，避免与监听器互相打转
    history.replaceState(null, "", hash);
  }
}

function applyView(view) {
  // 本地模式下书库不可用：地址栏直接写 #library 时改道去洞察
  if (view === "library" && state.local) {
    toast("本地模式下不浏览书库，已切到洞察");
    go("insights");
    return;
  }
  state.view = view;
  $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${view}`));
  $("#pageTitle").textContent = VIEW_META[view].title;
  try {
    $("#view-stats").classList.toggle("has-subtabs", view === "stats");
  } catch (err) {
    /* 结构缺失时忽略 */
  }
  paintActions();

  if (view === "library") {
    if (!state.loaded.library) loadFiles(state.path);
  }
  if (view === "device") {
    if (state.local) {
      toast("本地模式下不读取设备状态，关掉开关即可连接");
    } else {
      loadStatus();
    }
  }
  // 洞察与阅读记录只读缓存。与日历、单日同一套路：
  // 缓存还没确认过就先走一遍 /api/stats（联网时顺带拉一次新库），再取本页数据
  if (view === "insights") {
    if (!state.loaded.stats) loadStats().then(loadInsights);
    else loadInsights();
  }
  if (view === "records") {
    if (!state.loaded.stats) loadStats().then(loadSessions);
    else loadSessions();
  }
}

function applySub(sub, arg) {
  if (!SUBS.includes(sub)) sub = "overview";
  state.sub = sub;

  $$("#statsTabs button").forEach((b) => b.classList.toggle("is-on", b.dataset.sub === sub));
  $$(".subview").forEach((v) => v.classList.toggle("is-active", v.id === `sub-${sub}`));

  // 时间范围只服务概览与书籍两页；日历按月、单日按天、周按周，各管各的
  $("#rangeBar").hidden = !(sub === "overview" || sub === "books");

  const meta = SUB_META[sub];
  $("#pageSub").textContent = state.view === "stats" ? `${meta.title} · ${meta.sub}` : meta.sub;
  paintActions();

  if (sub === "overview" || sub === "books") {
    if (!state.loaded.stats) loadStats();
    else if (sub === "overview") relayoutCharts();
  } else if (sub === "week") {
    loadWeek(arg || state.weekStart || isoOf(new Date()));
  } else if (sub === "calendar") {
    if (arg && /^\d{4}-\d{2}$/.test(arg)) {
      const [y, m] = arg.split("-").map(Number);
      state.calMonth = { year: y, month: m };
    }
    if (!state.loaded.stats) {
      // 缓存还没确认过时，loadStats 可能要去设备拉库，这段空白也遮上
      showLoading($("#sub-calendar"), "正在读取阅读记录…");
      loadStats().then(() => loadCalendar(state.calMonth && state.calMonth.year,
                                          state.calMonth && state.calMonth.month));
    } else {
      loadCalendar(state.calMonth && state.calMonth.year, state.calMonth && state.calMonth.month);
    }
  } else if (sub === "day") {
    if (arg && /^\d{4}-\d{2}-\d{2}$/.test(arg)) state.selectedDate = arg;
    if (!state.loaded.stats) {
      showLoading($("#sub-day"), "正在读取阅读记录…");
      loadStats().then(() => loadDay(state.selectedDate));
    } else {
      loadDay(state.selectedDate);
    }
  }
}

function relayoutCharts() {
  setTimeout(() => {
    Object.values(state.charts).forEach((c) => c.resize());
    if (state.timeline) {
      try {
        state.timeline.redraw();
      } catch (err) {
        /* 已销毁 */
      }
    }
  }, 30);
}

function switchView(view) {
  go(view, view === "stats" ? state.sub : null, null);
}

function paintActions() {
  const box = $("#pageActions");
  box.innerHTML = "";
  if (state.view === "stats") {
    if (state.sub === "week") {
      box.appendChild(button("刷新数据", "btn btn-primary", () => loadWeek(state.weekStart, true)));
    } else if (state.sub === "calendar") {
      box.appendChild(button("刷新数据", "btn btn-primary", () => {
        loadCalendar(state.calMonth && state.calMonth.year, state.calMonth && state.calMonth.month);
      }));
    } else if (state.sub === "day") {
      box.appendChild(button("刷新数据", "btn btn-primary", () => loadDay(state.selectedDate)));
    } else {
      box.appendChild(button("刷新数据", "btn btn-primary", () => loadStats(true)));
    }
  } else if (state.view === "insights") {
    box.appendChild(button("刷新数据", "btn btn-primary", () => loadInsights()));
  } else if (state.view === "records") {
    box.appendChild(button("刷新", "btn", () => loadSessions()));
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
  $("#deviceDot").className = "dot " + (state.local ? "local" : online ? "online" : "offline");
  // 有些接口（如 /api/stats/week）不回传 host，不能因此把已有地址冲成「未配置」
  $("#chipHost").textContent = state.host || "未配置";
  $("#chipSub").textContent = state.local
    ? "本地模式 · 只读缓存"
    : online
      ? "已连接"
      : state.cacheOffline ? "离线 · 用缓存" : "未连接";
  updateBanner();
}

function updateBanner() {
  const banner = $("#offlineBanner");
  if (state.local) {
    banner.hidden = false;
    banner.classList.remove("warn");
    banner.classList.add("info");
    $("#offlineText").textContent = state.cachedAt
      ? `本地模式 · 正在看 ${fmtDateTime(state.cachedAt)} 的缓存，不会连接设备`
      : "本地模式 · 正在看本地缓存，不会连接设备";
    $("#bannerScan").hidden = true;
    return;
  }
  $("#bannerScan").hidden = false;
  banner.classList.remove("info");
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

/** 切换本地模式：同步状态、记忆与界面；关掉时顺手探一次设备。 */
function applyLocalMode(on) {
  state.local = on;
  localStorage.setItem("kwp.localMode", on ? "1" : "0");
  $("#localRow").setAttribute("aria-pressed", on ? "true" : "false");
  $("#localRow").classList.toggle("is-on", on);
  // 书库要连设备，本地模式下把入口藏起来，位置留给洞察与阅读记录
  $("#navLibrary").hidden = on;

  state.online = null;
  state.cacheOffline = false;
  if (on) {
    $("#deviceDot").className = "dot local";
    $("#chipSub").textContent = "本地模式 · 只读缓存";
    updateBanner();
  } else {
    $("#deviceDot").className = "dot";
    $("#chipSub").textContent = "检测中…";
    updateBanner();
    pingDevice();
  }
}

/** 切换模式后把当前这一页按新口径重新取一遍。 */
function reloadCurrent(force = true) {
  if (state.local && state.view === "library") {
    // 刚开本地模式时人还在书库：领去顶替它的洞察页，go 里已经取好数
    go("insights");
    return;
  }
  if (state.view === "stats") {
    if (state.sub === "overview" || state.sub === "books") {
      loadStats(force);
    } else if (state.sub === "week") {
      loadWeek(state.weekStart);
    } else if (state.sub === "calendar") {
      loadCalendar(state.calMonth && state.calMonth.year, state.calMonth && state.calMonth.month);
    } else if (state.sub === "day") {
      loadDay(state.selectedDate);
    }
  } else if (state.view === "insights") {
    loadInsights();
  } else if (state.view === "records") {
    loadSessions();
  } else if (state.view === "device" && !state.local) {
    loadStatus(true);
  } else if (state.view === "library" && !state.local) {
    loadFiles(state.path);
  }
}

/* ---------- 阅读统计：概览 ---------- */

let dbErrorShown = "";

/**
 * 任何统计接口返回都带 source/offline/cached_at，统一在这里同步在线状态。
 * 否则深链直接进周页、日历页时，从没请求过 /api/stats，横幅就一直误报。
 */
function syncOfflineState(data) {
  if (!data) return;
  if (data.db_error) {
    if (data.db_error !== dbErrorShown) {
      dbErrorShown = data.db_error;
      toast(`本地缓存库读不出来：${data.db_error}`, "err");
    }
  } else if (dbErrorShown) {
    dbErrorShown = "";
  }
  if (data.cached_at) state.cachedAt = data.cached_at;
  if (data.source === "local") {
    state.cacheOffline = false;
    // 首次加载前横幅拿不到 cached_at，这里补一次刷新
    updateBanner();
    return;
  }
  if (data.source === "device") {
    state.cacheOffline = false;
    setOnline(true, data.host);
  } else if (data.offline) {
    state.cacheOffline = true;
    setOnline(false, data.host);
  } else if (state.online === false) {
    state.cacheOffline = true;
    updateBanner();
  }
}

function offlineSuffix() {
  return state.cacheOffline ? ` · 缓存于 ${fmtDateTime(state.cachedAt)}` : "";
}

async function loadStats(force = false) {
  $("#pageSub").textContent = state.local ? "正在读取本地缓存…" : "正在从设备读取统计库…";
  try {
    const q = new URLSearchParams({ range: state.range });
    if (force) q.set("refresh", "1");
    if (state.local) q.set("local", "1");
    const data = await api(`/api/stats?${q.toString()}`);
    state.stats = data;
    state.loaded.stats = true;
    syncOfflineState(data);
    paintStats();
    if (state.local) {
      $("#pageSub").textContent = `本地模式 · 缓存于 ${fmtDateTime(data.cached_at)}`;
    } else if (state.cacheOffline) {
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

const RANGE_TITLE = {
  today: "本日阅读",
  week: "本周阅读",
  month: "本月阅读",
  year: "本年阅读",
  all: "累计阅读",
};

function paintStats() {
  const d = state.stats;
  if (!d) return;
  const o = d.overview;

  $("#kpiTotalLabel").textContent = RANGE_TITLE[d.range] || "阅读时长";
  $("#kpiTotal").textContent = fmtDuration(o.total_seconds);
  if (d.range === "all" || !d.all_time_seconds) {
    $("#kpiTotalFoot").textContent = "有记录以来";
  } else {
    const pct = Math.round((o.total_seconds * 100) / d.all_time_seconds);
    $("#kpiTotalFoot").textContent = `占全部阅读 ${pct}%`;
  }

  $("#kpiAvg").textContent = o.active_days ? `${o.avg_minutes} 分钟` : "—";
  $("#kpiAvgFoot").textContent = o.active_days
    ? `按 ${o.active_days} 个阅读日平均`
    : "暂无阅读记录";

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

/* ---------- 周视图 ---------- */

function fmtMD(iso) {
  return iso ? iso.slice(5) : "—";
}

async function loadWeek(start) {
  const host = $("#sub-week");
  showLoading(host, "正在读取这一周的记录…");
  try {
    const data = await api(`/api/stats/week${start ? `?start=${start}` : ""}`);
    state.week = data;
    state.weekStart = data.start;
    syncOfflineState(data);
    await nextFrame();
    paintWeek(data);
    const want = buildHash("stats", "week", data.start);
    if (state.view === "stats" && state.sub === "week" && location.hash !== want) {
      history.replaceState(null, "", want);
    }
    setTimeout(() => {
      if (state.charts.weekChart) state.charts.weekChart.resize();
    }, 30);
    await nextFrame();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    hideLoading(host);
  }
}

function shiftWeek(delta) {
  const base = parseDate(state.weekStart || isoOf(new Date()));
  base.setDate(base.getDate() + delta * 7);
  go("stats", "week", isoOf(base));
}

function paintWeek(w) {
  const o = w.overview;

  if (state.view === "stats" && state.sub === "week") {
    $("#pageSub").textContent = state.cacheOffline
      ? `周视图${offlineSuffix()}`
      : `周视图 · 这一周与上一周的对照`;
  }

  $("#wkRange").textContent = `${fmtMD(w.start)} ~ ${fmtMD(w.end)}`;
  $("#wkIso").textContent = w.is_current ? "本周" : `第 ${w.iso_week} 周`;

  $("#wkTotal").textContent = fmtDuration(o.seconds);
  $("#wkTotalFoot").textContent = o.seconds ? `${o.book_count} 本书` : "这一周没有记录";

  $("#wkAvg").textContent = o.avg_minutes ? `${o.avg_minutes} 分钟` : "—";
  $("#wkAvgFoot").textContent = o.active_days ? `按 ${o.active_days} 个阅读日` : "—";

  $("#wkPages").textContent = o.records.toLocaleString("zh-CN");
  $("#wkPagesFoot").textContent = `共 ${o.total_days} 天`;

  $("#wkDays").textContent = `${o.active_days} / ${o.total_days} 天`;
  const rest = o.total_days - o.active_days;
  $("#wkDaysFoot").textContent = rest ? `休了 ${rest} 天` : "全周满勤";

  const delta = $("#wkDelta");
  const deltaFoot = $("#wkDeltaFoot");
  if (w.delta_pct == null) {
    delta.textContent = "—";
    delta.classList.remove("is-up", "is-down");
    deltaFoot.textContent = "上周无记录";
  } else {
    const up = w.delta_pct >= 0;
    delta.textContent = `${up ? "+" : ""}${w.delta_pct}%`;
    delta.classList.toggle("is-up", up);
    delta.classList.toggle("is-down", !up);
    const diff = o.seconds - w.prev.seconds;
    deltaFoot.textContent = `${diff >= 0 ? "多" : "少"} ${fmtDuration(Math.abs(diff))}`;
  }

  $("#wkChartSub").textContent = `${fmtMD(w.start)} ~ ${fmtMD(w.end)} · 单位：分钟`;
  $("#wkBooksHint").textContent = o.book_count ? `${o.book_count} 本` : "—";

  paintWeekChart(w);
  paintWeekDays(w);
  paintWeekBooks(w);
}

function paintWeekChart(w) {
  const p = palette();
  const labels = w.days.map((d) => WEEK_NAMES[d.weekday - 1]);
  const cur = w.days.map((d) => d.minutes);
  const prev = (w.prev.days || []).map((d) => d.minutes);

  chartOf("weekChart", {
    backgroundColor: "transparent",
    grid: { left: 46, right: 16, top: 28, bottom: 30 },
    tooltip: {
      trigger: "axis",
      formatter: (ps) => {
        const i = ps[0].dataIndex;
        const d = w.days[i];
        return [
          `${WEEK_FULL[d.weekday - 1]} ${fmtMD(d.date)}`,
          `本周 ${cur[i]} 分钟 · ${d.records} 页`,
          `上周 ${prev[i] || 0} 分钟`,
        ].join("<br/>");
      },
      backgroundColor: p.panel,
      borderColor: p.border,
      textStyle: { color: p.text, fontSize: 12 },
    },
    legend: {
      right: 0,
      top: 0,
      itemWidth: 12,
      itemHeight: 8,
      textStyle: { color: p.muted, fontSize: 11 },
      data: ["本周", "上周"],
    },
    xAxis: {
      type: "category",
      data: labels,
      axisLine: { lineStyle: { color: p.border } },
      axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 12 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: p.border } },
      axisLabel: { color: p.muted, fontSize: 11 },
    },
    series: [
      {
        name: "本周",
        type: "bar",
        barMaxWidth: 40,
        data: cur,
        itemStyle: { color: p.accent, borderRadius: [4, 4, 0, 0] },
      },
      {
        name: "上周",
        type: "line",
        data: prev,
        symbol: "circle",
        symbolSize: 5,
        lineStyle: { color: p.muted, width: 1.5, type: "dashed" },
        itemStyle: { color: p.muted },
      },
    ],
  });
}

function paintWeekDays(w) {
  const box = $("#weekDays");
  box.innerHTML = "";
  let i = 0;
  while (i < 7) {
    const d = w.days[i];
    if (!d.seconds) {
      let j = i;
      while (j < 7 && !w.days[j].seconds) j += 1;
      const idle = w.days.slice(i, j);
      const first = idle[0];
      const last = idle[idle.length - 1];
      const label = idle.length === 1
        ? `${WEEK_FULL[first.weekday - 1]} ${fmtMD(first.date)}`
        : `${WEEK_FULL[first.weekday - 1]} ${fmtMD(first.date)} ~ ${WEEK_FULL[last.weekday - 1]} ${fmtMD(last.date)}`;
      const row = document.createElement("div");
      row.className = "week-day is-empty";
      row.innerHTML = `<span class="wd-date">${label}</span>
        <span class="wd-time">—</span>
        <span class="wd-pages">—</span>
        <span class="wd-books">未阅读</span>`;
      box.appendChild(row);
      i = j;
      continue;
    }
    const titles = d.books.slice(0, 2).map((b) => escapeHtml(b.title)).join("、");
    const more = d.books.length > 2 ? ` +${d.books.length - 2}` : "";
    const row = document.createElement("div");
    row.className = "week-day";
    row.innerHTML = `<span class="wd-date">${WEEK_FULL[d.weekday - 1]} ${fmtMD(d.date)}</span>
      <span class="wd-time">${fmtDuration(d.seconds)}</span>
      <span class="wd-pages">${d.records.toLocaleString("zh-CN")}</span>
      <span class="wd-books">${titles}${more}</span>`;
    row.onclick = () => go("stats", "day", d.date);
    box.appendChild(row);
    i += 1;
  }
}

function paintWeekBooks(w) {
  const box = $("#weekBooks");
  box.innerHTML = "";
  if (!w.books.length) {
    box.innerHTML = '<div class="empty">这一周没有阅读记录</div>';
    return;
  }
  const top = w.books[0].seconds || 1;
  w.books.forEach((b) => {
    const row = document.createElement("div");
    row.className = "week-book";
    const width = Math.max(2, Math.round((b.seconds / top) * 100));
    row.innerHTML = `<i class="swatch" style="background:${bookColor(b.id)}"></i>
      <span class="wb-title">${escapeHtml(b.title)}</span>
      <span class="wb-bar"><i style="width:${width}%"></i></span>
      <span class="wb-num">${fmtDuration(b.seconds)}</span>
      <span class="wb-share">${b.share}%</span>`;
    row.title = `读了 ${b.days} 天 · ${b.records} 页`;
    box.appendChild(row);
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
  const host = $("#sub-calendar");
  showLoading(host, `正在读取 ${year} 年 ${month} 月…`);
  try {
    const data = await api(`/api/stats/calendar?month=${year}-${String(month).padStart(2, "0")}`);
    state.calendar = data;
    syncOfflineState(data);
    await nextFrame();
    paintCalendar(data);
    await nextFrame();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    hideLoading(host);
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

    cell.onclick = () => openDay(d.date);
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
    if (dayDate) el.onclick = () => openDay(dayDate);
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

/** 从日历或周视图跳进单日页：切页 + 载入该日，地址栏一并更新。 */
function openDay(date) {
  state.selectedDate = date;
  go("stats", "day", date);
}

async function selectDate(date) {
  state.selectedDate = date;
  $$(".cal-day").forEach((c) => c.classList.toggle("is-sel", c.dataset.date === date));
  await loadDay(date);
}

/* ---------- 等待态 ---------- */

/* 转动的点由 SpinKit 提供，这里只管挂哪、说什么 */
const SPIN_CHASE_DOTS = Array.from({ length: 6 }, () => '<div class="sk-chase-dot"></div>').join("");

function veilOf(host) {
  let veil = host.querySelector(":scope > .loading-veil");
  if (!veil) {
    veil = document.createElement("div");
    veil.className = "loading-veil";
    veil.hidden = true;
    veil.innerHTML = `<div class="sk-chase">${SPIN_CHASE_DOTS}</div>
      <p class="loading-text"></p>`;
    host.appendChild(veil);
  }
  return veil;
}

function showLoading(host, text) {
  if (!host) return;
  const veil = veilOf(host);
  veil.querySelector(".loading-text").textContent = text || "正在读取…";
  veil.hidden = false;
  host.classList.add("is-loading");
  host.setAttribute("aria-busy", "true");
}

function hideLoading(host) {
  if (!host) return;
  const veil = host.querySelector(":scope > .loading-veil");
  if (veil) veil.hidden = true;
  host.classList.remove("is-loading");
  host.removeAttribute("aria-busy");
}

/* 等「这一帧真的画完了」再往下走，必须两个 rAF：
   第一个 rAF 的回调跑在绘制之前，回调里再排一个 setTimeout 也可能抢在绘制前执行，
   等于没等。两个 rAF 之间必定跨过一次真实绘制。
   这一点对 vis-timeline 尤其要紧——它的构造函数会一口气卡住 200~500ms，
   遮罩若没抢在那之前上屏，主人看到的就是「先空白、后 loading」。 */
const nextFrame = () => new Promise((resolve) => {
  requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
});

/* vis-timeline 分两阶段长：先轴和网格，隔一会儿才把条目摆上去。
   它画完会发一次 changed（实测正常速度 8ms、6 倍降速 60ms 后到），
   等这个事件，比盯 .vis-item 有没有宽高体面，也不依赖库的内部类名。
   万一事件没来（例如空数据），超时兜底，绝不把界面锁死。 */
function whenTimelineReady(timeline, timeout = 1200) {
  if (!timeline || typeof timeline.on !== "function") return Promise.resolve();
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      timeline.off("changed", finish);
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, timeout);
    timeline.on("changed", finish);
  });
}

/* ---------- 时间轴（手写） ---------- */

async function loadDay(date) {
  if (!date) date = state.selectedDate || isoOf(new Date());
  state.selectedDate = date;
  const host = $("#sub-day");
  showLoading(host, `正在读取 ${fmtMD(date)} 的阅读记录…`);
  try {
    const data = await api(`/api/stats/day?date=${date}`);
    state.day = data;
    syncOfflineState(data);
    await nextFrame();      // 遮罩先上屏，再去铺时间轴（那一步是同步的，会卡住主线程）
    paintTimeline(data);
    // 有柱子要画就等库把它画完；空日子本来就没有条目，不必等
    const bars = (data.books || []).reduce((n, b) => n + (b.periods ? b.periods.length : 0), 0);
    if (bars) await whenTimelineReady(state.timeline);
    await nextFrame();      // 让新内容先画出来，遮罩再撤：中间不留白
  } catch (err) {
    toast(err.message, "err");
  } finally {
    hideLoading(host);
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

/* ---------- 洞察 ---------- */

const PREV_NAME = { today: "昨天", week: "上周", month: "上月", year: "去年" };

/** 时间范围是概览与洞察共用的：一处切换，两处的高亮一起动。 */
function setRange(range) {
  state.range = range;
  paintRange();
  if (state.view === "stats") {
    if (state.sub === "overview" || state.sub === "books") loadStats();
  } else if (state.view === "insights") {
    loadInsights();
  }
}

function paintRange() {
  ["#rangeSeg", "#insRangeSeg"].forEach((sel) => {
    $$(sel + " button").forEach((b) => b.classList.toggle("is-on", b.dataset.range === state.range));
  });
}

async function loadInsights() {
  try {
    const data = await api(`/api/stats/insights?range=${state.range}`);
    state.insights = data;
    syncOfflineState(data);
    paintInsights(data);
    setTimeout(() => {
      ["monthChart", "heatChart"].forEach((id) => {
        if (state.charts[id]) state.charts[id].resize();
      });
    }, 30);
  } catch (err) {
    toast(err.message, "err");
  }
}

/* 一次阅读可能只有几秒钟，按分钟四舍五入会变成「0 分钟」 */
function fmtMinutes(seconds) {
  if (seconds > 0 && seconds < 30) return "不到 1 分钟";
  return fmtDuration(seconds);
}

function paintInsights(d) {
  const o = d.overview;
  if (state.view === "insights") {
    $("#pageSub").textContent = state.cacheOffline
      ? `洞察${offlineSuffix()}`
      : "规律与节奏 · 间隔不超过 15 分钟的翻页算同一次阅读";
  }

  $("#insTotalLabel").textContent = RANGE_TITLE[d.range] || "阅读时长";
  $("#insTotal").textContent = fmtDuration(o.seconds);
  let foot = o.seconds ? `${o.active_days} 个阅读日 · ${o.books} 本书` : "这个范围没有阅读记录";
  if (o.delta_pct != null) {
    foot += ` · 较${PREV_NAME[d.range]} ${o.delta_pct >= 0 ? "+" : ""}${o.delta_pct}%`;
  }
  $("#insTotalFoot").textContent = foot;

  $("#insAvg").textContent = o.session_count ? fmtMinutes(o.avg_session_seconds) : "—";
  $("#insAvgFoot").textContent = o.session_count ? `共 ${o.session_count} 次阅读` : "—";

  const lg = o.longest;
  $("#insLongest").textContent = lg ? fmtDuration(lg.seconds) : "—";
  $("#insLongestFoot").textContent = lg
    ? `${fmtMD(lg.date)} ${lg.start} – ${lg.end} · ${lg.books > 1 ? `${lg.books} 本书` : lg.title}`
    : "—";

  $("#insRangeHint").textContent = d.range_start ? `${d.range_start} 至今` : "有记录以来";
  $("#factsHint").textContent = o.speed ? `平均速度 ${o.speed} 页 / 小时` : "";

  paintMonthChart(d.monthly);
  paintHeatChart(d.heatmap);
  paintFacts(d.facts);
}

function paintMonthChart(monthly) {
  const p = palette();
  chartOf("monthChart", {
    backgroundColor: "transparent",
    grid: { left: 56, right: 12, top: 20, bottom: 28 },
    tooltip: {
      trigger: "axis",
      formatter: (ps) => {
        const m = monthly[ps[0].dataIndex];
        return `${m.month}<br/>${
          m.seconds ? `${fmtDuration(m.seconds)} · ${m.days} 个阅读日` : "没有阅读记录"}`;
      },
      backgroundColor: p.panel,
      borderColor: p.border,
      textStyle: { color: p.text, fontSize: 12 },
    },
    xAxis: {
      type: "category",
      data: monthly.map((m) => `${Number(m.month.slice(5))}月`),
      axisLine: { lineStyle: { color: p.border } },
      axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 11 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: p.border } },
      axisLabel: { color: p.muted, fontSize: 11, formatter: (v) => (v ? `${v} 小时` : "0") },
    },
    series: [{
      type: "bar",
      barMaxWidth: 18,
      data: monthly.map((m) => ({
        value: +(m.seconds / 3600).toFixed(2),
        itemStyle: {
          color: m.in_range ? p.accent : p.soft,
          borderRadius: [3, 3, 0, 0],
        },
      })),
    }],
  });
}

function paintHeatChart(heat) {
  const p = palette();
  const pad = (n) => String(n).padStart(2, "0");
  chartOf("heatChart", {
    backgroundColor: "transparent",
    grid: { left: 46, right: 12, top: 10, bottom: 28 },
    tooltip: {
      formatter: (ps) => {
        const [hour, weekday, minutes] = ps.data;
        return `${WEEK_FULL[weekday]} ${pad(hour)} 点<br/>${minutes ? `${minutes} 分钟` : "没有阅读"}`;
      },
      backgroundColor: p.panel,
      borderColor: p.border,
      textStyle: { color: p.text, fontSize: 12 },
    },
    xAxis: {
      type: "category",
      data: Array.from({ length: 24 }, (_, h) => pad(h)),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 11, interval: 2 },
    },
    yAxis: {
      type: "category",
      data: WEEK_FULL,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 11 },
    },
    visualMap: {
      show: false,
      min: 0,
      max: Math.max(1, heat.max_minutes),
      inRange: { color: [p.heatLow, p.soft, p.accent] },
    },
    series: [{
      type: "heatmap",
      data: heat.data,
      itemStyle: { borderColor: p.panel, borderWidth: 2, borderRadius: 3 },
      emphasis: { itemStyle: { borderColor: p.text, borderWidth: 1.5 } },
    }],
  });
}

function paintFacts(facts) {
  const box = $("#factsList");
  box.innerHTML = "";
  if (!facts.length) {
    box.innerHTML = '<div class="empty">这个范围还没有可总结的规律</div>';
    return;
  }
  facts.forEach((f) => {
    let value = "";
    let note = "";
    if (f.kind === "window") {
      value = f.start === f.end
        ? `${f.start} 点这一小时`
        : `${f.start} 点到 ${f.end < f.start ? "次日 " : ""}${f.end} 点`;
      note = `这 ${f.hours} 个小时占了 ${f.share}% 的阅读`;
    } else if (f.kind === "peak") {
      value = `${f.hour} 点这一小时`;
      note = `占 ${f.share}% 的阅读`;
    } else if (f.kind === "weekdays") {
      value = f.weekdays.map((w) => WEEK_FULL[w]).join("、");
      note = `${f.weekdays.length > 1 ? "合计占" : "占"} ${f.share}%`;
    } else if (f.kind === "speed_day") {
      value = `${fmtMD(f.date)} · ${f.speed} 页 / 小时`;
      note = `那天读了 ${fmtDuration(f.seconds)} · ${f.records} 页`;
    } else if (f.kind === "none") {
      value = "数据不足";
      note = f.note;
    } else if (f.kind === "top_book") {
      value = f.title;
      note = `占 ${f.share}% · ${fmtDuration(f.seconds)}`;
    }
    const row = document.createElement("div");
    row.className = "fact";
    row.innerHTML = `<span class="fact-label">${escapeHtml(f.label)}</span>
      <span class="fact-body"><b>${escapeHtml(value)}</b><span>${escapeHtml(note)}</span></span>`;
    box.appendChild(row);
  });
}

/* ---------- 阅读记录 ---------- */

const SESSION_PAGE = 100;    // 清单每次显示的条数，列表接口一次最多给 500 条

function sessionsQuery() {
  const q = new URLSearchParams({ period: state.sessionPeriod });
  if (state.sessionBook) q.set("book", state.sessionBook);
  return q;
}

async function loadSessions() {
  try {
    const data = await api(`/api/stats/sessions?${sessionsQuery().toString()}`);
    state.sessions = data;
    syncOfflineState(data);
    state.sessionRows = data.sessions;
    state.sessionShown = Math.min(SESSION_PAGE, data.sessions.length);
    paintBookOptions(data.books);
    paintSessions();
  } catch (err) {
    toast(err.message, "err");
  }
}

function paintBookOptions(books) {
  const sel = $("#recBook");
  sel.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部书籍";
  sel.appendChild(all);
  books.forEach((b) => {
    const opt = document.createElement("option");
    opt.value = String(b.id);
    opt.textContent = b.title;
    sel.appendChild(opt);
  });
  sel.value = state.sessionBook;
  if (sel.value !== state.sessionBook) {
    // 正选着的书不在可选范围里：退回全部书籍
    state.sessionBook = "";
    sel.value = "";
  }
}

function sessionRow(s) {
  const row = document.createElement("div");
  row.className = "sess-row";
  row.tabIndex = 0;
  const day = parseDate(s.date).getDay();
  const single = s.books.length === 1;
  const name = single ? s.books[0].title : `${s.books[0].title} 等 ${s.books.length} 本`;
  const swatch = single
    ? `<i class="swatch" style="background:${bookColor(s.books[0].id)}"></i>`
    : "";
  row.innerHTML = `<span class="sess-date">${fmtMD(s.date)}<em>${WEEK_FULL[(day + 6) % 7]}</em></span>
    <span class="sess-time">${s.start_time} – ${s.end_time}</span>
    <span class="sess-book">${swatch}<span title="${escapeHtml(name)}">${escapeHtml(name)}</span></span>
    <span class="sess-dur">${fmtMinutes(s.seconds)}</span>
    <span class="sess-pages">${s.records.toLocaleString("zh-CN")} 页</span>`;
  row.onclick = () => openDay(s.date);
  row.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openDay(s.date);
    }
  };
  return row;
}

function paintSessions() {
  const data = state.sessions;
  const rows = state.sessionRows;
  const box = $("#recList");
  box.innerHTML = "";
  if (state.view === "records") {
    $("#pageSub").textContent = data.total
      ? `共 ${data.total} 次阅读 · ${fmtDuration(data.seconds)} · ${data.records.toLocaleString("zh-CN")} 页`
      : "这个范围没有阅读记录";
  }
  if (!rows.length) {
    box.innerHTML = '<div class="empty">这个范围没有阅读记录</div>';
  } else {
    rows.slice(0, state.sessionShown).forEach((s) => box.appendChild(sessionRow(s)));
  }
  const rest = rows.length - state.sessionShown;
  $("#recMore").hidden = rest <= 0;
  $("#recMore").textContent = `再显示 ${Math.max(0, Math.min(SESSION_PAGE, rest))} 条`;
  $("#recMoreHint").textContent = data.truncated
    ? `只列出最近的 ${rows.length} 次，更早的请缩小范围或按书筛选`
    : "";
}

/** 清单由后端整份生成 CSV。走 save=1 让后端落盘并回真实路径，
 *  这样提示里说的是「确实写成了」，而不是「已开始导出」。 */
async function exportSessions() {
  const q = sessionsQuery();
  q.set("save", "1");
  try {
    const res = await fetch(`/api/stats/sessions/export?${q.toString()}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.detail || `导出失败（HTTP ${res.status}）`, "err");
      return;
    }
    toast(`已导出 ${data.rows} 次阅读 → data/exports/${data.name}`, "ok");
  } catch (err) {
    toast(`导出失败：${err.message}`, "err");
  }
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
  toast(`已请求下载 ${name}`, "ok");
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
    ["内存", `剩余 ${s.memory.free_mb} / 可用 ${s.memory.available_mb} / 总内存 ${s.memory.total_mb} MB`],
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
    if (state.local) applyLocalMode(false);
    switchView("device");
    scanDevices();
  };
  $("#localRow").onclick = () => {
    const on = !state.local;
    applyLocalMode(on);
    reloadCurrent();
    toast(on ? "已切到本地模式，不再连接设备" : "已切回联网模式");
  };
  $("#btnUpload").onclick = () => $("#fileInput").click();
  $("#fileInput").onchange = (e) => {
    uploadFiles(e.target.files);
    e.target.value = "";
  };
  $("#btnMkdir").onclick = newFolder;
  $("#btnSaveHost").onclick = saveHost;
  $("#btnScan").onclick = scanDevices;

  const onRange = (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    setRange(btn.dataset.range);
  };
  $("#rangeSeg").onclick = onRange;
  $("#insRangeSeg").onclick = onRange;

  $("#recPeriod").onchange = (e) => {
    state.sessionPeriod = e.target.value;
    loadSessions();
  };
  $("#recBook").onchange = (e) => {
    state.sessionBook = e.target.value;
    loadSessions();
  };
  $("#recMore").onclick = () => {
    state.sessionShown += SESSION_PAGE;
    paintSessions();
  };
  $("#recExport").onclick = exportSessions;

  $("#statsTabs").onclick = (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    go("stats", btn.dataset.sub, null);
  };

  $("#wkPrev").onclick = () => shiftWeek(-1);
  $("#wkNext").onclick = () => shiftWeek(1);
  $("#wkToday").onclick = () => {
    state.weekStart = null;
    go("stats", "week", null);
  };

  window.addEventListener("hashchange", () => {
    const route = parseHash();
    applyView(route.view);
    if (route.view === "stats") applySub(route.sub || "overview", route.arg || null);
  });

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
  $("#localRow").setAttribute("aria-pressed", state.local ? "true" : "false");
  $("#localRow").classList.toggle("is-on", state.local);
  $("#navLibrary").hidden = state.local;
  paintRange();

  if (state.local) {
    // 本地模式启动：不探活、不拉状态，直接读缓存
    $("#deviceDot").className = "dot local";
    $("#chipSub").textContent = "本地模式 · 只读缓存";
    updateBanner();
  } else {
    await pingDevice();
  }

  // 按地址栏落地：支持 #stats/week/2026-11-23 这类深链，缺省落概览
  const route = parseHash();
  go(route.view, route.sub || "overview", route.arg || null);
}

boot();
