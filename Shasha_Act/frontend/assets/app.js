(() => {
  const state = {
    token: "",
    settings: null,
    overview: null,
    health: null,
    positions: [],
    pendingEntries: [],
    events: [],
    history: [],
    historyTotal: 0,
    historyOffset: 0,
    historyLimit: 50,
    performance: null,
    market: null,
    marketFeed: [],
    marketSortKey: "change_pct",
    marketSortDir: "desc",
    marketFilterDir: "all",
    marketLimit: 350,
    prevMarketPrices: {},
    trendHistory: [],
    prevTrend: "Waiting...",
    currentView: "dashboard",
    performanceDays: 30,
    pollTimer: null,
    eventTimer: null,
    marketTimer: null,
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  const n = (value) => {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const fmtNum = (value, max = 6) => {
    const parsed = n(value);
    if (parsed === null) return "Unavailable";
    return new Intl.NumberFormat("en-IN", { maximumFractionDigits: max }).format(parsed);
  };
  const fmtPrice = (value) => {
    const parsed = n(value);
    if (parsed === null) return "Unavailable";
    const digits = Math.min(8, Math.max(2, Math.ceil(-Math.log10(Math.abs(parsed) || 1)) + 2));
    return new Intl.NumberFormat("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: 0 }).format(parsed);
  };
  const fmtMoney = (value, currency = "USDT", signed = false) => {
    const parsed = n(value);
    if (parsed === null) return "Unavailable";
    const prefix = signed && parsed > 0 ? "+" : "";
    return `${prefix}${new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(parsed)} ${currency}`;
  };
  const fmtDateTime = (value, options = {}) => {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    const tz = state.settings?.market_day_timezone || undefined;
    return new Intl.DateTimeFormat("en-IN", { timeZone: tz, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", ...options }).format(date);
  };
  const fmtTime = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("en-IN", { timeZone: state.settings?.market_day_timezone || undefined, hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
  };
  const setText = (selector, value) => { const element = $(selector); if (element) element.textContent = value; };
  const api = async (path, options = {}) => {
    const headers = { ...(options.headers || {}) };
    if (state.token) headers["X-Dashboard-Token"] = state.token;
    if (options.body && typeof options.body !== "string") {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json") ? await response.json() : await response.text();
    if (response.status === 401) {
      forgetToken();
      throw new Error("Dashboard authentication expired. Enter the access token again.");
    }
    if (!response.ok) {
      const message = typeof body === "object" && body ? (body.detail || body.message || `Request failed (${response.status})`) : `Request failed (${response.status})`;
      throw new Error(String(message));
    }
    return body;
  };
  const toast = (message, kind = "success") => {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    $("#toast-stack").append(node);
    window.setTimeout(() => node.remove(), 4200);
  };
  const badgeFor = (status) => {
    const value = String(status || "UNKNOWN").toUpperCase();
    if (["OPEN", "CLOSED", "ACTIVE", "NEW", "FILLED", "DELIVERED", "AVAILABLE", "CONNECTED", "OK", "RUNNING", "REST_FALLBACK", "SIMULATED_ACTIVE"].includes(value)) return "good";
    if (["REJECTED", "FAILED", "UNKNOWN", "PROTECTION_ERROR", "ALARM", "ERROR", "UNAVAILABLE", "STOPPED"].includes(value)) return "bad";
    if (["ENTRY_PENDING", "ENTRY_PARTIAL", "QUEUED", "PROCESSING", "CLOSING", "PARTIALLY_CLOSED", "SUBMITTING", "WAITING_FOR_FILL", "WAITING_FOR_SIMULATED_FILL", "NOT_CHECKED", "NOT_CONFIGURED", "DISCONNECTED", "DEGRADED", "DISABLED", "CANCELED", "CANCELLED", "PENDING_CONFIRMATION"].includes(value)) return "warn";
    return "muted-pill";
  };
  const pill = (text, kind = null) => `<span class="pill ${kind || badgeFor(text)}">${esc(text || "UNKNOWN")}</span>`;
  const coinIcon = (symbol) => {
    const base = String(symbol || "?").replace(/USDT$/i, "").slice(0, 3).toUpperCase() || "?";
    return `<span class="coin-icon">${esc(base)}</span>`;
  };
  const blankRow = (cols, message) => `<tr><td colspan="${cols}" class="empty-cell">${esc(message)}</td></tr>`;

  function showAuth(message = "") {
    $("#auth-overlay").classList.remove("hidden");
    $("#auth-error").textContent = message;
    $("#auth-token").value = "";
    $("#auth-token").focus();
  }
  function forgetToken() {
    state.token = "";
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
    showAuth("");
  }

  async function authenticate(token) {
    state.token = token.trim();
    try {
      const settings = await api("/api/settings");
      state.settings = settings;
      $("#auth-overlay").classList.add("hidden");
      await refreshAll(true);
      if (!state.pollTimer) state.pollTimer = setInterval(() => refreshAll(false), 15000);
      return true;
    } catch (error) {
      state.token = "";
      showAuth(error.message || "Authentication failed.");
      return false;
    }
  }

  async function refreshAll(initial = false) {
    if (!state.token) return;
    const jobs = [
      ["settings", api("/api/settings")],
      ["overview", api("/api/overview")],
      ["positions", api("/api/positions")],
      ["pendingEntries", api("/api/pending-entries")],
      ["health", api("/api/health")],
      ["events", api("/api/events?limit=12")],
      ["performance", api(`/api/performance?days=${state.performanceDays}`)],
    ];
    const results = await Promise.allSettled(jobs.map(([, promise]) => promise));
    let failures = 0;
    results.forEach((result, index) => {
      if (result.status === "fulfilled") state[jobs[index][0]] = result.value;
      else {
        failures += 1;
        if (jobs[index][0] === "pendingEntries") state.pendingEntries = null;
      }
    });
    if (failures && initial) toast("Some dashboard data is currently unavailable; stale values are not reused.", "error");
    renderDashboard();
    renderSettings();
    renderPositions();
    renderPendingEntries();
    renderEvents();
    renderPerformance();
    if (state.currentView === "markets") await loadMarkets().catch((error) => toast(error.message, "error"));
    if (state.currentView === "history") await loadHistory().catch((error) => toast(error.message, "error"));
    setText("#last-updated", fmtTime(new Date().toISOString()));
    const now = new Date();
    const tz = state.settings?.market_day_timezone || "Asia/Kolkata";
    setText("#market-clock-label", `MARKET DAY · ${tz.toUpperCase()}`);
    setText("#market-clock", new Intl.DateTimeFormat("en-IN", { timeZone: tz, hour: "2-digit", minute: "2-digit" }).format(now));
  }

  function renderDashboard() {
    const overview = state.overview;
    if (overview) {
      const day = overview.day_pnl || {};
      const pnl = n(day.realized_net);
      const kpi = $("#kpi-day-pnl");
      kpi.textContent = pnl === null ? "Unavailable" : fmtMoney(pnl, day.currency || "USDT", true);
      kpi.classList.toggle("positive", pnl !== null && pnl > 0);
      kpi.classList.toggle("negative", pnl !== null && pnl < 0);
      setText("#kpi-day-pnl-note", `${overview.market_day} · ${overview.market_day_timezone} · ${day.funding_status === "AVAILABLE" ? "funding included" : "funding unavailable"}`);
      setText("#kpi-open", overview.open_positions ?? "—");
      setText("#kpi-open-note", `${overview.pending_entries ?? 0} pending entries`);
      setText("#kpi-signals", overview.signals_today ?? "—");
      setText("#kpi-signal-note", `${overview.trades_today ?? 0} trade reservation(s) today · ${overview.mode}`);
      const closed = Number(overview.closed_today || 0);
      setText("#kpi-win-rate", closed ? `${Math.round((Number(overview.wins_today || 0) / closed) * 100)}%` : "—");
      setText("#kpi-win-note", `${overview.wins_today || 0} wins · ${overview.losses_today || 0} losses · ${closed} closed`);
      setText("#nav-open-count", overview.open_positions ?? "—");
      setText("#positions-count", `${overview.open_positions ?? 0}`);
      setText("#positions-footer", `${overview.pending_entries ?? 0} pending LIMIT entries are not counted as open exposure`);
    }
    if (state.settings) {
      const mode = state.settings.effective_mode || "PAPER";
      setModePill(mode);
      setText("#market-clock-label", `MARKET DAY · ${state.settings.market_day_timezone || "UTC"}`);
    }
    if (state.health) renderHealth();
    renderDashboardPositions();
    renderDashboardEvents();
    const warning = state.health && (
      state.health.status !== "OK" ||
      state.health.components?.market_data === "DEGRADED" ||
      state.health.components?.database !== "OK" ||
      Number(state.health.outbox?.failed || 0) > 0 ||
      Number(state.overview?.unknown_or_attention || 0) > 0
    );
    const alert = $("#system-alert");
    alert.classList.toggle("hidden", !warning);
    if (warning) {
      setText("#system-alert-title", "System attention required");
      const details = [];
      if (state.health.components?.database !== "OK") details.push("database unavailable");
      if (state.health.components?.market_data === "DEGRADED" || state.health.components?.market_data === "DISCONNECTED") details.push("market data stale or disconnected");
      if (state.health.outbox?.failed) details.push(`${state.health.outbox.failed} failed outbox job(s)`);
      if (state.overview?.unknown_or_attention) details.push(`${state.overview.unknown_or_attention} trade(s) need attention`);
      setText("#system-alert-copy", details.join(" · ") || "Review system status before further action.");
    }
  }

  function setModePill(mode) {
    const className = mode === "LIVE" ? "mode-pill live" : "mode-pill paper";
    ["#mode-pill", "#settings-mode-pill"].forEach((selector) => {
      const element = $(selector);
      if (!element) return;
      element.className = className;
      element.innerHTML = `<i></i><span>${mode === "LIVE" ? "LIVE MODE" : "PAPER MODE"}</span>`;
    });
  }

  function renderHealth() {
    const health = state.health;
    if (!health) return;
    const components = health.components || {};
    const services = [
      ["Database", components.database || "UNKNOWN"],
      ["Outbox worker", components.worker || "UNKNOWN"],
      ["Market data", components.market_data || "UNKNOWN"],
      ["Public WebSocket", components.websocket || "UNKNOWN"],
      ["Shark private API", components.private_api || "UNKNOWN"],
      ["Telegram notifier", components.telegram || "UNKNOWN"],
    ];
    const rows = services.map(([name, value]) => {
      let kind = "warn";
      if (["OK", "RUNNING", "CONNECTED", "REST_FALLBACK", "CONFIGURED"].includes(value)) kind = "good";
      if (["ERROR", "UNAVAILABLE", "STOPPED"].includes(value)) kind = "bad";
      const label = value.replaceAll("_", " ");
      return `<div class="service-row"><span class="service-name"><i class="status-dot ${kind === "good" ? "ok" : kind === "bad" ? "bad" : "warn"}"></i>${esc(name)}</span><span class="service-state ${kind}">${esc(label)}</span><span class="service-latency">${name === "Market data" && state.market?.last_success ? esc(fmtTime(state.market.last_success)) : ""}</span></div>`;
    }).join("");
    $("#service-list").innerHTML = rows;
    const ok = health.status === "OK" && !Number(health.outbox?.failed || 0) && !Number(state.overview?.unknown_or_attention || 0);
    const overall = $("#system-overall");
    overall.innerHTML = `<span class="system-orbit"><i></i></span><div><strong>${ok ? "All checks operational" : "Attention recommended"}</strong><small>${esc(health.components?.market_data || "Market data unavailable")} · ${esc(health.outbox?.pending ?? 0)} queued · ${esc(health.outbox?.failed ?? 0)} failed</small></div>`;
    setText("#health-checked-at", fmtTime(health.timestamp));
    const feed = components.market_data || "UNKNOWN";
    const dot = $("#sidebar-dot");
    dot.className = `status-dot ${["CONNECTED", "REST_FALLBACK"].includes(feed) ? "ok" : feed === "DEGRADED" ? "warn" : "bad"}`;
    setText("#sidebar-health", feed.replaceAll("_", " "));
    setText("#sidebar-market", `WebSocket ${components.websocket || "unknown"} · API ${components.database || "unknown"}`);
    $("#clock-dot").className = `status-dot ${["CONNECTED", "REST_FALLBACK"].includes(feed) ? "ok" : "warn"}`;
  }

  function renderDashboardPositions() {
    const items = state.positions || [];
    const target = $("#dashboard-position-rows");
    if (!items.length) {
      target.innerHTML = blankRow(8, "No open positions or unresolved exposures.");
      return;
    }
    target.innerHTML = items.slice(0, 6).map((item) => {
      const pnl = n(item.unrealized_pnl);
      const pnlClass = pnl === null ? "neutral" : pnl >= 0 ? "positive" : "negative";
      const protection = item.protection_state || "UNKNOWN";
      const stop = item.stop_price ? fmtPrice(item.stop_price) : "Unavailable";
      return `<tr><td><div class="contract-cell">${coinIcon(item.symbol)}<div class="contract-copy"><strong>${esc(item.symbol)}</strong><small>${esc(item.mode)} · ${esc(item.direction)}</small></div></div></td><td><div class="dual-value"><strong>${fmtPrice(item.actual_entry || item.planned_entry)}</strong><small>last ${fmtPrice(item.last_price)}</small></div></td><td><div class="dual-value"><strong>${fmtNum(item.remaining_quantity)}</strong><small>qty left</small></div></td><td>${esc(item.leverage)}×</td><td class="${pnlClass}">${pnl === null ? "Unavailable" : fmtMoney(pnl, "USDT", true)}</td><td>${pill(protection)}</td><td>${pill(item.status)}</td><td><button class="icon-button tiny" title="Trade details" data-action="details" data-id="${esc(item.id)}">···</button></td></tr>`;
    }).join("");
  }

  function renderDashboardEvents() {
    const items = state.events || [];
    const target = $("#dashboard-event-rows");
    if (!items.length) {
      target.innerHTML = blankRow(7, "No TradingView events have been recorded yet.");
      return;
    }
    target.innerHTML = items.slice(0, 8).map((event) => {
      const jobs = event.outbox || [];
      const notification = jobs.filter((job) => job.kind === "TELEGRAM_SIGNAL");
      const notifyText = notification.length ? notification.map((job) => job.status).join(", ") : "—";
      return `<tr><td>${esc(fmtDateTime(event.received_at))}</td><td><div class="dual-value"><strong>${esc(event.strategy || "SHASHA")}</strong><small>${esc(event.timeframe || "timeframe unavailable")}</small></div></td><td><div class="dual-value"><strong>${esc(event.tradingview_symbol || "Unavailable")}</strong><small>${esc(event.symbol || "contract unverified")}</small></div></td><td><div class="dual-value"><strong>${fmtPrice(event.entry_price)}</strong><small>signal close ${fmtPrice(event.signal_close)}</small></div></td><td><span class="mode-mini">${esc(event.mode || "PAPER")}</span></td><td>${pill(event.status)}</td><td>${pill(notifyText, notification.some((job) => job.status === "FAILED") ? "bad" : "muted-pill")}</td></tr>`;
    }).join("");
  }

  function renderPerformance() {
    const performance = state.performance;
    if (!performance || !Array.isArray(performance.series)) return;
    const series = performance.series;
    setText("#performance-mode", performance.mode || "PAPER");
    setText("#performance-period", `Last ${performance.days} days · fees included · funding not included`);
    const sum = series.reduce((total, item) => total + (n(item.net_realized_pnl) || 0), 0);
    const totalEl = $("#performance-total");
    totalEl.textContent = fmtMoney(sum, "USDT", true);
    totalEl.classList.toggle("positive", sum > 0);
    totalEl.classList.toggle("negative", sum < 0);
    if (!series.length) return;
    const values = series.map((item) => n(item.net_realized_pnl) || 0);
    const highest = Math.max(0, ...values);
    const lowest = Math.min(0, ...values);
    const range = highest - lowest || 1;
    const zeroPercent = (highest / range) * 100;
    const chart = $("#performance-chart");
    chart.innerHTML = values.map((value, index) => {
      const height = value === 0 ? 1.5 : Math.max(2, (Math.abs(value) / range) * 96);
      const style = value >= 0 ? `bottom:${100 - zeroPercent}%;height:${height}%` : `top:${zeroPercent}%;height:${height}%`;
      const title = `${series[index].date}: ${fmtMoney(value)}`;
      return `<div class="chart-bar-slot" title="${esc(title)}"><span class="chart-bar ${value < 0 ? "negative" : ""}" style="${style}"></span></div>`;
    }).join("");
    const zero = $("#chart-zero");
    zero.style.display = "block";
    zero.style.top = `${zeroPercent}%`;
    setText("#chart-max", fmtNum(highest, 2));
    setText("#chart-mid", fmtNum((highest + lowest) / 2, 2));
    setText("#chart-min", fmtNum(lowest, 2));
    setText("#chart-start", series[0].date.slice(5));
    setText("#chart-end", series.at(-1).date.slice(5));
  }

  function renderPendingEntries() {
    const target = $("#pending-entry-rows");
    if (!target) return;
    if (!state.pendingEntries) {
      setText("#pending-entry-count", "Unavailable");
      target.innerHTML = blankRow(7, "Pending-entry data is unavailable; refresh before taking action.");
      return;
    }
    const items = state.pendingEntries.items || [];
    setText("#pending-entry-count", `${items.length}`);
    if (!items.length) {
      target.innerHTML = blankRow(7, "No queued or pending limit entries.");
      return;
    }
    target.innerHTML = items.map((item) => {
      const canCancel = ["ENTRY_PENDING", "ENTRY_PARTIAL"].includes(item.status);
      const action = canCancel
        ? `<button class="button button-danger button-small" data-action="cancel-entry" data-id="${esc(item.id)}">Cancel entry</button>`
        : `<span class="status-reason">Awaiting order confirmation</span>`;
      return `<tr><td><div class="contract-cell">${coinIcon(item.symbol)}<div class="contract-copy"><strong>${esc(item.symbol)}</strong><small>${esc(item.mode)} · ${esc(item.direction)}</small></div></div></td><td><div class="dual-value"><strong>${fmtPrice(item.planned_entry)}</strong><small>signal close ${fmtPrice(item.signal_close)}</small></div></td><td><div class="dual-value"><strong>${fmtNum(item.quantity)}</strong><small>${item.status === "ENTRY_PARTIAL" ? `filled · ${fmtNum(item.remaining_quantity)} open` : "planned"}</small></div></td><td>${esc(fmtDateTime(item.entry_expires_at))}</td><td><span class="mode-mini">${esc(item.mode)}</span></td><td>${pill(item.status)}${item.status_reason ? `<small class="status-reason">${esc(item.status_reason)}</small>` : ""}</td><td>${action}</td></tr>`;
    }).join("");
  }

  function renderPositions() {
    const items = state.positions || [];
    const target = $("#position-rows");
    const overview = state.overview;
    const summary = $("#position-summary");
    if (summary && overview) {
      summary.innerHTML = `<div class="summary-card"><span>OPEN / ATTENTION</span><strong>${esc(overview.open_positions ?? "—")}</strong></div><div class="summary-card"><span>PENDING ENTRIES</span><strong>${esc(overview.pending_entries ?? "—")}</strong></div><div class="summary-card"><span>UNRESOLVED</span><strong>${esc(overview.unknown_or_attention ?? "—")}</strong></div><div class="summary-card"><span>MODE</span><strong>${esc(overview.mode || "—")}</strong></div>`;
    }
    if (!target) return;
    if (!items.length) {
      target.innerHTML = blankRow(11, "No positions or unresolved trades are currently recorded.");
      return;
    }
    target.innerHTML = items.map((item) => {
      const pnl = n(item.unrealized_pnl);
      const pnlClass = pnl === null ? "neutral" : pnl >= 0 ? "positive" : "negative";
      const targets = `T1 ${fmtPrice(item.target1_price)}<br>T2 ${fmtPrice(item.target2_price)}<br>T3 ${fmtPrice(item.target3_price)}`;
      const isPaperOpen = item.mode === "PAPER" && ["OPEN", "PARTIALLY_CLOSED"].includes(item.status);
      const buttons = `<button class="button button-ghost button-small" data-action="details" data-id="${esc(item.id)}">Details</button>${isPaperOpen ? `<button class="button button-ghost button-small" data-action="edit-risk" data-id="${esc(item.id)}">Edit</button>` : ""}${["OPEN", "PARTIALLY_CLOSED"].includes(item.status) ? `<button class="button button-danger button-small" data-action="close" data-id="${esc(item.id)}">Close</button>` : ""}`;
      return `<tr><td><div class="contract-cell">${coinIcon(item.symbol)}<div class="contract-copy"><strong>${esc(item.symbol)}</strong><small>${esc(item.mode)} · ${esc(item.direction)}</small></div></div></td><td><div class="dual-value"><strong>${fmtPrice(item.planned_entry)}</strong><small>fill ${fmtPrice(item.actual_entry)}</small></div></td><td><div class="dual-value"><strong>${fmtPrice(item.last_price)}</strong><small>${item.price_stale ? "stale / unavailable" : esc(item.price_source || "last price")}</small></div></td><td>${fmtNum(item.remaining_quantity)}</td><td><div class="dual-value"><strong>${fmtMoney(item.margin)}</strong><small>notional ${fmtMoney(item.notional)}</small></div></td><td class="${pnlClass}"><div class="dual-value"><strong>${pnl === null ? "Unavailable" : fmtMoney(pnl, "USDT", true)}</strong><small>${item.roi_percent_on_margin === null ? "ROI unavailable" : `${fmtNum(item.roi_percent_on_margin, 2)}% on margin`}</small></div></td><td>${fmtPrice(item.liquidation_price)}</td><td><div class="dual-value"><strong>SL ${fmtPrice(item.stop_price)}</strong><small>${targets}</small></div></td><td>${pill(item.protection_state)}</td><td>${pill(item.status)}</td><td><div class="row-actions">${buttons}</div></td></tr>`;
    }).join("");
  }

  function fmtVol(value) {
    if (!value) return "—";
    const num = Number(value);
    if (!Number.isFinite(num)) return "—";
    if (num >= 1_000_000_000) return (num / 1_000_000_000).toFixed(2) + "B";
    if (num >= 1_000_000) return (num / 1_000_000).toFixed(2) + "M";
    if (num >= 1_000) return (num / 1_000).toFixed(1) + "K";
    return num.toFixed(2);
  }

  function rangeBarHtml(low, high, last) {
    const l = Number(low);
    const h = Number(high);
    const cur = Number(last);
    if (!h || !l || h === l || !Number.isFinite(cur)) {
      return `<div class="range-bar-track"><div class="range-bar-fill" style="width: 50%"></div></div>`;
    }
    const pct = Math.min(100, Math.max(0, ((cur - l) / (h - l)) * 100));
    const tone = pct > 50 ? "up" : "down";
    return `
      <div class="range-bar-cell">
        <div class="range-bar-track">
          <div class="range-bar-fill ${tone}" style="width: ${pct.toFixed(1)}%"></div>
        </div>
        <div class="range-bar-labels">
          <span>L</span>
          <span>${pct.toFixed(0)}%</span>
          <span>H</span>
        </div>
      </div>
    `;
  }

  function dayRangeBarHtml(low, high, last) {
    const l = Number(low);
    const h = Number(high);
    const cur = Number(last);
    if (!h || !l || h === l || !Number.isFinite(cur)) {
      return `<span class="muted">—</span>`;
    }
    const pct = Math.min(100, Math.max(0, ((cur - l) / (h - l)) * 100));
    const tone = pct > 50 ? "up" : "down";
    return `
      <div class="range-bar-cell">
        <div class="range-bar-track">
          <div class="range-bar-fill ${tone}" style="width: ${pct.toFixed(1)}%"></div>
        </div>
        <div class="range-bar-labels">
          <span class="negative">${fmtPrice(l)}</span>
          <span>${pct.toFixed(0)}%</span>
          <span class="positive">${fmtPrice(h)}</span>
        </div>
      </div>
    `;
  }

  function priceCellHtml(symbol, price) {
    const cur = Number(price);
    const prev = state.prevMarketPrices[symbol];
    let flashCls = "";
    if (prev !== undefined && Number.isFinite(cur) && cur !== prev) {
      flashCls = cur > prev ? "flash-up" : "flash-down";
    }
    state.prevMarketPrices[symbol] = cur;
    return `<span class="price-val ${flashCls}">${fmtPrice(cur)}</span>`;
  }

  async function updateSentiment(gainers, losers, totalValid) {
    if (totalValid === 0) return { label: "Waiting...", cls: "neutral" };
    const gPct = gainers / totalValid;
    const lPct = losers / totalValid;
    let label = "Neutral";
    let cls = "neutral";
    if (gPct > 0.55) { label = "Bullish"; cls = "bullish"; }
    else if (lPct > 0.55) { label = "Bearish"; cls = "bearish"; }

    if (label !== "Waiting..." && label !== state.prevTrend) {
      if (state.prevTrend !== "Waiting...") {
        const from = state.prevTrend;
        const to = label;
        api("/api/market/sentiment-history", {
          method: "POST",
          body: { from, to },
        }).then((saved) => {
          if (saved) {
            state.trendHistory = [{ time: saved.time, from, to }, ...state.trendHistory].slice(0, 50);
            renderSentimentHistory();
          }
        }).catch(() => {
          state.trendHistory = [{ time: new Date().toISOString(), from, to }, ...state.trendHistory].slice(0, 50);
          renderSentimentHistory();
        });
      }
      state.prevTrend = label;
    }
    return { label, cls };
  }

  function renderSentimentHistory() {
    const list = $("#market-sentiment-history");
    const count = $("#sentiment-history-count");
    if (!list) return;
    if (count) setText(count, `${state.trendHistory.length} transition(s)`);
    if (!state.trendHistory.length) {
      list.innerHTML = `<div class="muted-note">No sentiment transitions recorded yet.</div>`;
      return;
    }
    list.innerHTML = state.trendHistory.map((h) => {
      const cls = h.to === "Bullish" ? "bullish" : h.to === "Bearish" ? "bearish" : "neutral";
      return `
        <div class="sentiment-transition-row">
          <span class="trans-time">${new Date(h.time).toLocaleTimeString()}</span>
          <span class="trans-from">${esc(h.from)}</span>
          <span class="trans-arrow">→</span>
          <span class="trans-to ${cls}">${esc(h.to)}</span>
        </div>
      `;
    }).join("");
  }

  async function loadSentimentHistory() {
    try {
      const rows = await api("/api/market/sentiment-history?limit=50");
      if (Array.isArray(rows)) {
        state.trendHistory = rows;
        if (rows.length > 0 && state.prevTrend === "Waiting...") {
          state.prevTrend = rows[0].to;
        }
        renderSentimentHistory();
      }
    } catch (e) {
      // Ignore network errors
    }
  }

  async function loadMarkets(showSpinner = false) {
    const refreshBtn = $("#market-refresh-btn");
    if (showSpinner && refreshBtn) refreshBtn.classList.add("loading");
    try {
      const limit = Number($("#market-limit")?.value || 350);
      state.marketLimit = limit;
      const data = await api(`/api/market/live-feed?limit=${limit}`);
      state.marketFeed = Array.isArray(data) ? data : [];
      renderMarketsFeed();
      const statusEl = $("#market-feed-status");
      const statusText = $("#market-status-text");
      if (statusEl) statusEl.className = "market-feed-status good";
      if (statusText) setText(statusText, `Connected ${new Date().toLocaleTimeString()}`);
    } catch (error) {
      const statusEl = $("#market-feed-status");
      const statusText = $("#market-status-text");
      if (statusEl) statusEl.className = "market-feed-status bad";
      if (statusText) setText(statusText, "Disconnected");
      toast(error.message, "error");
    } finally {
      if (refreshBtn) refreshBtn.classList.remove("loading");
    }
  }

  async function renderMarketsFeed() {
    const feed = state.marketFeed || [];
    const search = ($("#market-search")?.value || "").trim().toUpperCase();
    const filterDir = state.marketFilterDir || "all";
    const sortKey = state.marketSortKey || "change_pct";
    const sortDir = state.marketSortDir || "desc";

    let rows = feed.filter((r) => r.status === "ok" && Number(r.last_price) > 0);
    
    // Summary metrics before search filter
    const totalValid = rows.length;
    const gainers = rows.filter((r) => Number(r.change_pct) > 0).length;
    const losers = rows.filter((r) => Number(r.change_pct) < 0).length;
    const totalVol = rows.reduce((s, r) => s + (Number(r.volume) || 0), 0);
    
    const sentiment = await updateSentiment(gainers, losers, totalValid);
    
    // Update KPI Cards
    const card = $("#market-sentiment-card");
    if (card) card.className = `panel market-stat-card sentiment-stat ${sentiment.cls}`;
    setText("#market-sentiment-label", sentiment.label);
    setText("#market-total-symbols", totalValid);
    setText("#market-gainers-count", gainers);
    setText("#market-losers-count", losers);
    setText("#market-total-vol", fmtVol(totalVol));

    // Filter by direction
    if (filterDir === "up") rows = rows.filter((r) => Number(r.change_pct) > 0);
    if (filterDir === "down") rows = rows.filter((r) => Number(r.change_pct) < 0);

    // Filter by search
    if (search) {
      rows = rows.filter((r) => (r.symbol && r.symbol.toUpperCase().includes(search)) || (r.base_asset && r.base_asset.toUpperCase().includes(search)));
    }

    // Sort
    rows.sort((a, b) => {
      let av, bv;
      if (sortKey === "hl_range") {
        av = (!a.high || !a.low || a.high === a.low) ? 50 : ((a.last_price - a.low) / (a.high - a.low)) * 100;
        bv = (!b.high || !b.low || b.high === b.low) ? 50 : ((b.last_price - b.low) / (b.high - b.low)) * 100;
      } else if (sortKey === "day_hl_range") {
        av = (!a.day_high || !a.day_low || a.day_high === a.day_low) ? 50 : ((a.last_price - a.day_low) / (a.day_high - a.day_low)) * 100;
        bv = (!b.day_high || !b.day_low || b.day_high === b.day_low) ? 50 : ((b.last_price - b.day_low) / (b.day_high - b.day_low)) * 100;
      } else {
        av = a[sortKey];
        bv = b[sortKey];
      }
      if (typeof av === "number" && typeof bv === "number") {
        return sortDir === "asc" ? av - bv : bv - av;
      }
      return sortDir === "asc" ? String(av || "").localeCompare(String(bv || "")) : String(bv || "").localeCompare(String(av || ""));
    });

    setText("#market-showing-count", `${rows.length} showing`);

    // Update active sort indicator on table headers
    $$(".market-feed-table th.sortable").forEach((th) => {
      const col = th.dataset.sort;
      if (col === sortKey) {
        th.classList.add("active-sort");
        const arr = th.querySelector(".sort-arr");
        if (arr) setText(arr, sortDir === "asc" ? "▲" : "▼");
      } else {
        th.classList.remove("active-sort");
        const arr = th.querySelector(".sort-arr");
        if (arr) setText(arr, "↕");
      }
    });

    const tbody = $("#market-rows");
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = blankRow(11, "No symbols match your filter.");
      return;
    }

    tbody.innerHTML = rows.map((row, i) => {
      const change = Number(row.change_pct) || 0;
      const changeTone = change > 0 ? "up" : change < 0 ? "down" : "flat";
      const changeIcon = change > 0 ? "▲" : change < 0 ? "▼" : "—";
      const baseShort = (row.base_asset || row.symbol || "").slice(0, 2);
      
      const dayChange = Number(row.day_change_pct) || 0;
      const dayTone = dayChange > 0 ? "up" : dayChange < 0 ? "down" : "flat";
      const dayIcon = dayChange > 0 ? "▲" : dayChange < 0 ? "▼" : "—";
      
      return `
        <tr>
          <td class="muted w-num">${i + 1}</td>
          <td>
            <div class="contract-cell">
              <div class="asset-badge ${changeTone}">${esc(baseShort)}</div>
              <div class="contract-copy">
                <strong>${esc(row.base_asset || row.symbol)}</strong>
                <small>${esc(row.symbol)}</small>
              </div>
            </div>
          </td>
          <td>${priceCellHtml(row.symbol, row.last_price)}</td>
          <td>
            <span class="pct-pill ${changeTone}">
              <span>${changeIcon}</span>
              <span>${change >= 0 ? "+" : ""}${change.toFixed(3)}%</span>
            </span>
          </td>
          <td class="positive font-mono">${fmtPrice(row.high)}</td>
          <td class="negative font-mono">${fmtPrice(row.low)}</td>
          <td>${rangeBarHtml(row.low, row.high, row.last_price)}</td>
          <td>
            ${row.day_open > 0 ? `
              <span class="pct-pill ${dayTone}">
                <span>${dayIcon}</span>
                <span>${dayChange >= 0 ? "+" : ""}${dayChange.toFixed(3)}%</span>
              </span>
            ` : `<span class="muted">—</span>`}
          </td>
          <td>${dayRangeBarHtml(row.day_low, row.day_high, row.last_price)}</td>
          <td class="font-mono muted">${fmtVol(row.volume)}</td>
          <td>
            ${row.webhook_status ? `
              <span class="badge ${row.webhook_status === 'BUY' ? 'badge-green' : row.webhook_status === 'SELL' ? 'badge-red' : 'badge-blue'}">
                ${esc(row.webhook_status)}
              </span>
            ` : `<span class="muted">—</span>`}
          </td>
        </tr>
      `;
    }).join("");
  }

  async function loadEvents() {
    const status = $("#event-status-filter")?.value || "";
    const symbol = $("#event-symbol-filter")?.value || "";
    const params = new URLSearchParams({ limit: "100" });
    if (status) params.set("status", status);
    if (symbol) params.set("symbol", symbol.toUpperCase());
    const data = await api(`/api/events?${params.toString()}`);
    state.events = data.items || [];
    renderEvents();
    renderDashboardEvents();
    setText("#signal-total", `${data.total ?? 0} event(s)`);
  }
  function renderEvents() {
    const rows = $("#signal-rows");
    if (!rows) return;
    const items = state.events || [];
    if (!items.length) {
      rows.innerHTML = blankRow(8, "No signal events match this filter.");
      return;
    }
    rows.innerHTML = items.map((event) => {
      const jobs = event.outbox || [];
      const jobSummary = jobs.length ? jobs.map((job) => `${esc(String(job.kind || "").replaceAll("_", " "))} · ${esc(job.status)}${job.attempts ? ` (${esc(job.attempts)})` : ""}`).join("<br>") : "No outbox action";
      const failed = jobs.some((job) => job.kind === "TELEGRAM_SIGNAL" && job.status === "FAILED");
      return `<tr><td>${esc(fmtDateTime(event.received_at))}</td><td><div class="dual-value"><strong>${esc(event.strategy || "SHASHA")}</strong><small>${esc((event.id || "").slice(0, 8))} · ${esc(event.timeframe || "timeframe unavailable")}</small></div></td><td><div class="dual-value"><strong>${esc(event.tradingview_symbol || "Unavailable")}</strong><small>Shark: ${esc(event.symbol || "unresolved")}</small></div></td><td><div class="dual-value"><strong>signal ${fmtPrice(event.signal_close)}</strong><small>entry ${fmtPrice(event.entry_price)}</small></div></td><td><span class="mode-mini">${esc(event.mode || "PAPER")}</span></td><td><div class="dual-value">${pill(event.status)}<small class="status-reason">${esc(event.status_reason || "")}</small></div></td><td class="status-reason">${jobSummary}</td><td>${failed ? `<button class="button button-secondary button-small" data-action="retry-notice" data-id="${esc(event.id)}">Retry notice</button>` : ""}</td></tr>`;
    }).join("");
    setText("#signal-total", `${state.events.length} event(s) on this page`);
  }

  async function loadHistory() {
    const mode = $("#history-mode")?.value || "";
    const symbol = $("#history-symbol")?.value || "";
    const params = new URLSearchParams({ limit: String(state.historyLimit), offset: String(state.historyOffset) });
    if (mode) params.set("mode", mode);
    if (symbol) params.set("symbol", symbol.toUpperCase());
    const data = await api(`/api/history?${params.toString()}`);
    state.history = data.items || [];
    state.historyTotal = data.total || 0;
    renderHistory();
    const exportParams = new URLSearchParams();
    if (mode) exportParams.set("mode", mode);
    if (symbol) exportParams.set("symbol", symbol.toUpperCase());
    $("#csv-export").href = `/api/history.csv${exportParams.size ? `?${exportParams.toString()}` : ""}`;
  }
  function renderHistory() {
    const rows = $("#history-rows");
    if (!rows) return;
    const items = state.history || [];
    if (!items.length) rows.innerHTML = blankRow(10, "No trades match the selected filters.");
    else rows.innerHTML = items.map((trade) => {
      const pnl = n(trade.net_realized_pnl);
      const cls = pnl === null ? "neutral" : pnl >= 0 ? "positive" : "negative";
      const fillLabel = trade.fee_data_available ? "Fee data available" : "Fees incomplete";
      return `<tr><td>${esc(fmtDateTime(trade.created_at))}</td><td><div class="contract-cell">${coinIcon(trade.symbol)}<div class="contract-copy"><strong>${esc(trade.symbol)}</strong><small>${esc(trade.tradingview_symbol)}</small></div></div></td><td><span class="mode-mini">${esc(trade.mode)}</span></td><td><div class="dual-value"><strong>${fmtPrice(trade.actual_entry || trade.planned_entry)}</strong><small>exit ${fmtPrice(trade.exit_price)}</small></div></td><td>${fmtNum(trade.quantity)}</td><td class="${cls}">${pnl === null ? "Unavailable" : fmtMoney(pnl, "USDT", true)}</td><td>${fmtMoney(trade.fees_paid)}</td><td>${pill(fillLabel, trade.fee_data_available ? "good" : "warn")}</td><td>${pill(trade.status)}</td><td><button class="button button-ghost button-small" data-action="details" data-id="${esc(trade.id)}">Details</button></td></tr>`;
    }).join("");
    setText("#history-total", `${state.historyTotal} record(s)`);
    const page = Math.floor(state.historyOffset / state.historyLimit) + 1;
    setText("#history-page-label", `Page ${page}`);
    $("#history-prev").disabled = state.historyOffset <= 0;
    $("#history-next").disabled = state.historyOffset + state.historyLimit >= state.historyTotal;
    setText("#history-caption", `Most recent ${state.historyLimit} records · ${state.historyTotal} total`);
  }

  function renderSettings() {
    const settings = state.settings;
    if (!settings) return;
    setModePill(settings.effective_mode || "PAPER");
    const risk = settings.risk || {};
    const form = $("#risk-form");
    for (const [key, value] of Object.entries(risk)) {
      const input = form.elements.namedItem(key);
      if (!input) continue;
      if (input.type === "checkbox") input.checked = Boolean(value);
      else if (Array.isArray(value)) continue;
      else if (document.activeElement !== input) input.value = value ?? "";
    }
    const symbols = $("#symbol-form");
    if (symbols && document.activeElement !== symbols.elements.allow_symbols) symbols.elements.allow_symbols.value = (risk.allow_symbols || []).join(", ");
    if (symbols && document.activeElement !== symbols.elements.deny_symbols) symbols.elements.deny_symbols.value = (risk.deny_symbols || []).join(", ");
    setText("#timezone-value", settings.market_day_timezone || "Unavailable");
    setText("#paper-assumption", `Starting simulation balance: ${fmtMoney(settings.paper_initial_balance)}\nMaker fee: ${fmtNum(settings.paper_maker_fee_bps, 2)} bps · Taker fee: ${fmtNum(settings.paper_taker_fee_bps, 2)} bps`);
    const checkList = [
      ["Server LIVE gate", settings.live_server_gate_configured],
      ["Shark credentials configured", settings.shark_credentials_configured],
      ["Contract filters verified", settings.live_contract_rules_verified],
      ["Split TP/SL semantics verified", settings.live_split_protection_verified],
    ];
    $("#live-checks").innerHTML = checkList.map(([label, ok]) => `<span class="check-chip ${ok ? "ok" : "no"}">${ok ? "✓" : "!"} ${esc(label)}</span>`).join("");
    const available = Boolean(settings.live_available);
    $("#enable-live-button").disabled = !available;
    setText("#live-blocker-copy", available ? "Server gates and current market rules appear verified. LIVE still requires an explicit confirmation phrase." : "LIVE is blocked until server-side gates, contract tick/step filters, and split TP/SL semantics are independently verified.");
    $("#webhook-config-badge").textContent = settings.webhook_configured ? "Token configured" : "Token not configured";
    $("#webhook-config-badge").className = `badge ${settings.webhook_configured ? "badge-green" : "badge-amber"}`;
    const paused = Boolean(settings.trading_paused);
    const stateEl = $("#kill-switch-state");
    stateEl.className = `kill-switch-state ${paused ? "paused" : "active"}`;
    stateEl.innerHTML = `<span class="status-dot"></span><strong>${paused ? "New signals paused" : "Signal processing active"}</strong>`;
    const button = $("#settings-pause-button");
    button.textContent = paused ? "Resume new trades" : "Pause new trades";
    button.className = `button ${paused ? "button-primary" : "button-danger"} full-width`;
    button.disabled = Boolean(settings.environment_pause) && paused;
    const services = [
      ["Dashboard authentication", settings.dashboard_auth_configured],
      ["TradingView webhook", settings.webhook_configured],
      ["Telegram", settings.telegram_configured],
      ["Shark private credentials", settings.shark_credentials_configured],
    ];
    $("#config-service-list").innerHTML = services.map(([name, configured]) => `<div class="config-service"><span>${esc(name)}</span><strong class="${configured ? "ok" : "no"}">${configured ? "CONFIGURED" : "NOT CONFIGURED"}</strong></div>`).join("");
  }

  function switchView(view) {
    if (!$("#view-" + view)) return;
    state.currentView = view;
    $$(".page").forEach((page) => page.classList.toggle("active", page.id === `view-${view}`));
    $$(".nav-item[data-view]").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    const labels = { dashboard: "Overview", positions: "Positions", markets: "Markets", signals: "Signal log", history: "Trade history", settings: "Settings" };
    setText("#page-crumb", labels[view] || view);
    $("#sidebar").classList.remove("open");
    if (view === "markets") {
      loadMarkets().catch((error) => toast(error.message, "error"));
      loadSentimentHistory();
    }
    if (view === "history") loadHistory().catch((error) => toast(error.message, "error"));
    if (view === "signals") loadEvents().catch((error) => toast(error.message, "error"));
    if (view === "positions") renderPositions();
  }

  function openConfirm({ title, copy, phrase, confirmText = "Confirm", acknowledge = false, onConfirm }) {
    const modal = $("#modal-backdrop");
    setText("#modal-title", title);
    setText("#modal-copy", copy);
    setText("#modal-phrase-display", phrase || "");
    $("#modal-phrase-label").classList.toggle("hidden", !phrase);
    $("#modal-phrase-input").value = "";
    $("#modal-phrase-input").placeholder = phrase ? "Enter confirmation phrase" : "";
    $("#modal-ack-row").classList.toggle("hidden", !acknowledge);
    $("#modal-ack").checked = false;
    $("#modal-confirm").textContent = confirmText;
    $("#modal-confirm").disabled = Boolean(phrase) || acknowledge;
    modal.classList.remove("hidden");
    const check = () => {
      const phraseOk = !phrase || $("#modal-phrase-input").value === phrase;
      const ackOk = !acknowledge || $("#modal-ack").checked;
      $("#modal-confirm").disabled = !(phraseOk && ackOk);
    };
    const input = $("#modal-phrase-input");
    const ack = $("#modal-ack");
    input.oninput = check;
    ack.onchange = check;
    $("#modal-cancel").onclick = closeModal;
    $("#modal-close").onclick = closeModal;
    $("#modal-confirm").onclick = async () => {
      $("#modal-confirm").disabled = true;
      try {
        await onConfirm();
        closeModal();
      } catch (error) {
        toast(error.message, "error");
        check();
      }
    };
    window.setTimeout(() => (phrase ? input.focus() : $("#modal-confirm").focus()), 20);
  }
  function closeModal() { $("#modal-backdrop").classList.add("hidden"); }

  async function openTradeDetails(tradeId) {
    $("#drawer-backdrop").classList.remove("hidden");
    $("#drawer-title").textContent = "Loading trade…";
    $("#drawer-body").innerHTML = `<div class="drawer-empty"><span class="spinner"></span>Loading audit trail</div>`;
    try {
      const [item, orders, audit] = await Promise.all([
        api(`/api/trades/${encodeURIComponent(tradeId)}`),
        api(`/api/trades/${encodeURIComponent(tradeId)}/orders`),
        api(`/api/trades/${encodeURIComponent(tradeId)}/audit`),
      ]);
      $("#drawer-title").textContent = `${item.symbol} · ${item.status}`;
      const row = (label, value) => `<div class="detail-row"><span>${esc(label)}</span><strong>${esc(value ?? "Unavailable")}</strong></div>`;
      const orderRows = (orders.items || []).map((order) => row(`${order.kind} · ${order.status}`, `${order.side} ${fmtNum(order.filled_quantity)} / ${fmtNum(order.quantity)} @ ${fmtPrice(order.price)}`)).join("") || `<div class="drawer-empty">No order records.</div>`;
      const auditRows = (audit.items || []).map((entry) => row(`${entry.action} · ${fmtDateTime(entry.created_at)}`, JSON.stringify(entry.details || {}))).join("") || `<div class="drawer-empty">No audit events.</div>`;
      const canEdit = item.mode === "PAPER" && ["OPEN", "PARTIALLY_CLOSED"].includes(item.status);
      const riskForm = canEdit ? `<section class="drawer-section"><h3>EDIT PAPER RISK LEVELS</h3><form id="drawer-risk-form" class="settings-form"><label class="phrase-label">Stop price<input name="stop_price" type="number" min="0.00000001" step="any" value="${esc(item.stop_price || "")}"></label><label class="phrase-label">Target 1<input name="target1_price" type="number" min="0.00000001" step="any" value="${esc(item.target1_price || "")}"></label><label class="phrase-label">Target 2<input name="target2_price" type="number" min="0.00000001" step="any" value="${esc(item.target2_price || "")}"></label><label class="phrase-label">Target 3<input name="target3_price" type="number" min="0.00000001" step="any" value="${esc(item.target3_price || "")}"></label><label class="phrase-label">Type CONFIRM RISK CHANGE<input name="confirmation_phrase" type="text" autocomplete="off"></label><button class="button button-secondary" type="submit">Apply risk edits</button></form></section>` : "";
      $("#drawer-body").innerHTML = `<section class="drawer-section"><h3>POSITION</h3>${row("Mode / status", `${item.mode} / ${item.status}`)}${row("TradingView → Shark", `${item.tradingview_symbol} → ${item.symbol}`)}${row("Signal close", fmtPrice(item.signal_close))}${row("Planned entry", fmtPrice(item.planned_entry))}${row("Actual entry", fmtPrice(item.actual_entry))}${row("Quantity / remaining", `${fmtNum(item.quantity)} / ${fmtNum(item.remaining_quantity)}`)}${row("Margin / notional", `${fmtMoney(item.margin)} / ${fmtMoney(item.notional)}`)}${row("Last price", fmtPrice(item.last_price))}${row("Unrealized P&L", item.unrealized_pnl === null ? "Unavailable" : fmtMoney(item.unrealized_pnl, "USDT", true))}${row("Protection", `${item.protection_state} · ${item.stop_stage}`)}${row("Active stop", fmtPrice(item.stop_price))}${row("Targets", `T1 ${fmtPrice(item.target1_price)} · T2 ${fmtPrice(item.target2_price)} · T3 ${fmtPrice(item.target3_price)}`)}${row("Liquidation price", fmtPrice(item.liquidation_price))}${row("Exchange position ID", item.exchange_position_id)}</section>${riskForm}<section class="drawer-section"><h3>ORDER HISTORY</h3>${orderRows}</section><section class="drawer-section"><h3>AUDIT TRAIL</h3>${auditRows}</section>`;
      const form = $("#drawer-risk-form");
      if (form) form.onsubmit = async (event) => {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(form).entries());
        ["stop_price", "target1_price", "target2_price", "target3_price"].forEach((key) => { if (data[key] === "") delete data[key]; });
        try {
          await api(`/api/trades/${encodeURIComponent(tradeId)}/risk`, { method: "PATCH", body: data });
          toast("Paper risk levels updated.");
          closeDrawer();
          await refreshAll(false);
        } catch (error) { toast(error.message, "error"); }
      };
    } catch (error) {
      $("#drawer-body").innerHTML = `<div class="drawer-empty">${esc(error.message)}</div>`;
    }
  }
  function closeDrawer() { $("#drawer-backdrop").classList.add("hidden"); }

  async function closePosition(tradeId) {
    openConfirm({
      title: "Close position",
      copy: "A PAPER position is simulated at the latest fresh Shark public last price. A LIVE close, if enabled, is reduce-only and remains pending until Shark confirms it. This action does not guarantee a fill price.",
      phrase: "CLOSE POSITION",
      confirmText: "Submit close",
      onConfirm: async () => {
        const result = await api(`/api/trades/${encodeURIComponent(tradeId)}/close`, { method: "POST", body: { confirmation_phrase: "CLOSE POSITION" } });
        toast(result.message || "Close action submitted.");
        await refreshAll(false);
      },
    });
  }

  async function cancelPendingEntry(tradeId) {
    const entry = (state.pendingEntries?.items || []).find((item) => item.id === tradeId);
    const partial = entry?.status === "ENTRY_PARTIAL";
    openConfirm({
      title: "Cancel pending entry",
      copy: `This requests cancellation of the pending limit order. Only confirmed cancellations are counted. ${partial ? "Any already-filled quantity remains open; this action does not close that exposure." : "Cancellation is not final until the exchange or simulator confirms it."}`,
      phrase: "CANCEL PENDING ENTRIES",
      confirmText: "Request cancellation",
      onConfirm: async () => {
        const result = await api(`/api/trades/${encodeURIComponent(tradeId)}/cancel-entry`, {
          method: "POST",
          body: { confirmation_phrase: "CANCEL PENDING ENTRIES" },
        });
        toast(result.message, result.confirmed_cancellations ? "success" : "error");
        await refreshAll(false);
      },
    });
  }

  async function retryNotice(eventId) {
    const result = await api(`/api/events/${encodeURIComponent(eventId)}/retry-notification`, { method: "POST" });
    toast(result.message || "Notification retry queued.");
    await loadEvents();
  }

  async function saveRiskForm(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const status = $("#risk-save-status");
    status.textContent = "Saving…";
    status.className = "form-status";
    const body = {};
    for (const name of ["margin_per_trade", "leverage", "max_open_positions", "max_notional_per_trade", "target1_percent", "target2_percent", "target3_percent", "stop_loss_percent", "max_daily_loss", "limit_entry_expiry_seconds"]) body[name] = form.elements[name].value;
    body.cancel_stale_entry_orders = form.elements.cancel_stale_entry_orders.checked;
    try {
      await api("/api/settings", { method: "PATCH", body });
      status.textContent = "Saved";
      toast("Risk settings saved for future trades.");
      await refreshAll(false);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
    }
  }

  async function saveSymbolForm(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const status = $("#symbol-save-status");
    status.textContent = "Saving…";
    status.className = "form-status";
    const csv = (value) => value.split(",").map((item) => item.trim()).filter(Boolean);
    try {
      await api("/api/settings", { method: "PATCH", body: { allow_symbols: csv(form.elements.allow_symbols.value), deny_symbols: csv(form.elements.deny_symbols.value) } });
      status.textContent = "Saved";
      toast("Symbol filters saved.");
      await refreshAll(false);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
    }
  }

  function bindEvents() {
    $("#auth-form").addEventListener("submit", (event) => { event.preventDefault(); authenticate($("#auth-token").value); });
    $("#signout-button").addEventListener("click", forgetToken);
    $("#refresh-button").addEventListener("click", () => refreshAll(true));
    $("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
    $("#dismiss-alert").addEventListener("click", () => $("#system-alert").classList.add("hidden"));
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#drawer-backdrop").addEventListener("click", (event) => { if (event.target.id === "drawer-backdrop") closeDrawer(); });
    $("#modal-backdrop").addEventListener("click", (event) => { if (event.target.id === "modal-backdrop") closeModal(); });
    $("#modal-cancel").addEventListener("click", closeModal);
    $$("[data-view]").forEach((element) => element.addEventListener("click", () => switchView(element.dataset.view)));
    $("#performance-range").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-days]");
      if (!button) return;
      state.performanceDays = Number(button.dataset.days);
      $$("#performance-range button").forEach((item) => item.classList.toggle("selected", item === button));
      try { state.performance = await api(`/api/performance?days=${state.performanceDays}`); renderPerformance(); }
      catch (error) { toast(error.message, "error"); }
    });
    $("#market-search")?.addEventListener("input", () => {
      renderMarketsFeed();
    });
    $("#market-limit")?.addEventListener("change", () => {
      loadMarkets(true);
    });
    $("#market-refresh-btn")?.addEventListener("click", () => {
      loadMarkets(true);
    });
    $("#market-filter-dir")?.addEventListener("click", (event) => {
      const btn = event.target.closest("button");
      if (!btn) return;
      state.marketFilterDir = btn.dataset.dir || "all";
      $$("#market-filter-dir button").forEach((b) => b.classList.toggle("selected", b === btn));
      renderMarketsFeed();
    });
    $(".market-feed-table thead")?.addEventListener("click", (event) => {
      const th = event.target.closest("th.sortable");
      if (!th) return;
      const col = th.dataset.sort;
      if (state.marketSortKey === col) {
        state.marketSortDir = state.marketSortDir === "asc" ? "desc" : "asc";
      } else {
        state.marketSortKey = col;
        state.marketSortDir = "desc";
      }
      renderMarketsFeed();
    });
    $("#event-status-filter").addEventListener("change", () => loadEvents().catch((error) => toast(error.message, "error")));
    $("#event-symbol-filter").addEventListener("input", () => {
      clearTimeout(state.eventTimer);
      state.eventTimer = setTimeout(() => loadEvents().catch((error) => toast(error.message, "error")), 250);
    });
    $("#signals-refresh").addEventListener("click", () => loadEvents().catch((error) => toast(error.message, "error")));
    $("#history-mode").addEventListener("change", () => { state.historyOffset = 0; loadHistory().catch((error) => toast(error.message, "error")); });
    $("#history-symbol").addEventListener("input", () => {
      clearTimeout(state.eventTimer);
      state.eventTimer = setTimeout(() => { state.historyOffset = 0; loadHistory().catch((error) => toast(error.message, "error")); }, 250);
    });
    $("#history-prev").addEventListener("click", () => { state.historyOffset = Math.max(0, state.historyOffset - state.historyLimit); loadHistory().catch((error) => toast(error.message, "error")); });
    $("#history-next").addEventListener("click", () => { state.historyOffset += state.historyLimit; loadHistory().catch((error) => toast(error.message, "error")); });
    $("#csv-export").addEventListener("click", async (event) => {
      event.preventDefault();
      try {
        const response = await fetch(event.currentTarget.href, { headers: { "X-Dashboard-Token": state.token }, credentials: "same-origin", cache: "no-store" });
        if (!response.ok) throw new Error(`CSV export failed (${response.status})`);
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "shasha-act-history.csv";
        link.click();
        URL.revokeObjectURL(url);
      } catch (error) { toast(error.message, "error"); }
    });
    $("#risk-form").addEventListener("submit", saveRiskForm);
    $("#symbol-form").addEventListener("submit", saveSymbolForm);
    $("#cancel-pending-button").addEventListener("click", () => openConfirm({
      title: "Cancel pending entries",
      copy: "The system will cancel every outstanding entry request. The count shown afterward includes only cancellations confirmed by Shark; unknown outcomes remain visible for reconciliation.",
      phrase: "CANCEL PENDING ENTRIES",
      confirmText: "Request cancellations",
      onConfirm: async () => {
        const result = await api("/api/control/cancel-pending", { method: "POST", body: { confirmation_phrase: "CANCEL PENDING ENTRIES" } });
        toast(`${result.confirmed_cancellations} cancellation(s) confirmed.`);
        await refreshAll(false);
      },
    }));
    $("#pause-button").addEventListener("click", promptPause);
    $("#settings-pause-button").addEventListener("click", promptPause);
    $("#switch-paper-button").addEventListener("click", () => openConfirm({
      title: "Switch to PAPER mode",
      copy: "PAPER is the safe default. Switching is blocked while any LIVE position or unresolved LIVE trade remains.",
      phrase: "SWITCH TO PAPER",
      confirmText: "Switch to PAPER",
      onConfirm: async () => {
        await api("/api/control/mode/paper", { method: "POST", body: { confirmation_phrase: "SWITCH TO PAPER" } });
        toast("Trading mode set to PAPER.");
        await refreshAll(false);
      },
    }));
    $("#enable-live-button").addEventListener("click", () => openConfirm({
      title: "Enable LIVE trading",
      copy: "This can place real orders using your Shark account. Verify server-side gates, exchange filters, protection behavior and risk settings before proceeding.",
      phrase: "ENABLE LIVE TRADING",
      confirmText: "Enable LIVE",
      acknowledge: true,
      onConfirm: async () => {
        await api("/api/control/mode/live", { method: "POST", body: { confirmation_phrase: "ENABLE LIVE TRADING", acknowledge_exchange_risk: $("#modal-ack").checked } });
        toast("LIVE mode activation confirmed.", "error");
        await refreshAll(false);
      },
    }));
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-action]");
      if (!button) return;
      const { action, id } = button.dataset;
      if (action === "details" || action === "edit-risk") openTradeDetails(id);
      if (action === "close") closePosition(id);
      if (action === "cancel-entry") cancelPendingEntry(id);
      if (action === "retry-notice") retryNotice(id).catch((error) => toast(error.message, "error"));
    });
  }

  function promptPause() {
    const paused = Boolean(state.settings?.trading_paused);
    if (paused && state.settings?.environment_pause) {
      toast("Server emergency pause is active and cannot be cleared in the dashboard.", "error");
      return;
    }
    const phrase = paused ? "RESUME SHASHA ACT" : "PAUSE SHASHA ACT";
    openConfirm({
      title: paused ? "Resume signal processing" : "Pause new trading",
      copy: paused ? "New valid signals will be admitted again. Existing orders and positions are not changed." : "New signal reservations will be blocked. Existing open trades and pending exchange orders are not automatically closed or canceled.",
      phrase,
      confirmText: paused ? "Resume signals" : "Pause new trades",
      onConfirm: async () => {
        await api("/api/control/pause", { method: "POST", body: { paused: !paused, confirmation_phrase: phrase } });
        toast(paused ? "Signal processing resumed." : "New signal processing paused.");
        await refreshAll(false);
      },
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    const pathView = window.location.hash.slice(1);
    if (["dashboard", "positions", "markets", "signals", "history", "settings"].includes(pathView)) switchView(pathView);
    showAuth("");
  });
})();
