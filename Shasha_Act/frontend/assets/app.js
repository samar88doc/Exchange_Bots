/* =====================================================================================
   Shasha Act — neural operations console
   -------------------------------------------------------------------------------------
   Contract notes (must stay true):
   * Every request is same-origin and relative. No absolute host is ever hard-coded.
   * Session auth sends `X-Session-Token`; the server automation token sends
     `X-Dashboard-Token`. Both are explicit headers, never cookies, so no CSRF token
     is required and an embedded frame stays usable.
   * Any value the server did not confirm is rendered as `Unavailable` instead of 0.
   * This file is loaded as an external asset because the server ships a strict
     `script-src 'self'` content policy: no inline script, no remote bundle.
   ===================================================================================== */
"use strict";

(() => {
  const SESSION_STORAGE_KEY = "shasha_session_token";
  const TOKEN_STORAGE_KEY = "shasha_dashboard_token";
  const PREF_STORAGE_KEY = "shasha_console_prefs";
  const POLL_MS = 15000;

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  /* ------------------------------------------------------------------ formatting -- */
  const esc = (value) => String(value === null || value === undefined ? "" : value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");

  const isMissing = (value) => value === null || value === undefined || value === "";

  const toNumber = (value) => {
    if (isMissing(value)) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };

  /** Decimal-safe price rendering: never round-trips through a binary float. */
  const decimalsFor = (value) => {
    const text = String(value ?? "");
    const dot = text.indexOf(".");
    if (dot < 0) return 0;
    return Math.min(text.length - dot - 1, 12);
  };

  const fmtPrice = (value, fallback = "Unavailable") => {
    if (isMissing(value)) return fallback;
    const digits = Math.max(decimalsFor(value), 2);
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return esc(value);
    return numeric.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  };

  const fmtNum = (value, digits = 4, fallback = "Unavailable") => {
    const numeric = toNumber(value);
    if (numeric === null) return fallback;
    return numeric.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: digits });
  };

  const fmtMoney = (value, currency = "USDT", signed = false, fallback = "Unavailable") => {
    const numeric = toNumber(value);
    if (numeric === null) return fallback;
    const formatted = `${numeric.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency}`;
    return signed && numeric > 0 ? `+${formatted}` : formatted;
  };

  const fmtPercent = (value, digits = 2, fallback = "Unavailable") => {
    const numeric = toNumber(value);
    if (numeric === null) return fallback;
    return `${numeric > 0 ? "+" : ""}${numeric.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
  };

  const fmtCompact = (value, fallback = "Unavailable") => {
    const numeric = toNumber(value);
    if (numeric === null) return fallback;
    return numeric.toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 1 });
  };

  const fmtDateTime = (value, fallback = "Unavailable") => {
    if (isMissing(value)) return fallback;
    const moment = new Date(value);
    if (Number.isNaN(moment.getTime())) return fallback;
    return moment.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };

  const fmtClock = (value, fallback = "Unavailable") => {
    if (isMissing(value)) return fallback;
    const moment = new Date(value);
    if (Number.isNaN(moment.getTime())) return fallback;
    const seconds = Math.max(Math.round((Date.now() - moment.getTime()) / 1000), 0);
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
  };

  const relativeDays = (value) => {
    if (isMissing(value)) return null;
    const target = new Date(value).getTime() - Date.now();
    if (!Number.isFinite(target)) return null;
    return Math.floor(target / 86400000);
  };

  const coinIcon = (symbol) => {
    const text = String(symbol || "?");
    const base = text.replace(/(USDT|USD|USDC|PERP)$/i, "").slice(0, 3) || text.slice(0, 2);
    return `<span class="coin-icon">${esc(base.toUpperCase())}</span>`;
  };

  const pnlClass = (value) => {
    const numeric = toNumber(value);
    if (numeric === null) return "muted";
    return numeric > 0 ? "positive" : numeric < 0 ? "negative" : "";
  };

  const pill = (label, tone = "muted") => {
    const mapping = { good: "good", ok: "good", bad: "bad", warn: "warn", muted: "muted-pill", info: "muted-pill" };
    return `<span class="pill ${mapping[tone] || "muted-pill"}">${esc(String(label ?? "UNKNOWN").replaceAll("_", " "))}</span>`;
  };

  const statusTone = (status) => {
    const value = String(status || "").toUpperCase();
    if (["OPEN", "CLOSED", "ACTIVE", "OK", "CONNECTED", "DELIVERED", "ACCEPTED", "PROCESSED"].includes(value)) return "good";
    if (["PARTIALLY_CLOSED", "ENTRY_PENDING", "QUEUED", "SUBMITTING", "TRIAL", "EXPIRING_SOON", "REST_FALLBACK", "PROCESSING", "PENDING", "NOT_CHECKED", "DISABLED"].includes(value)) return "warn";
    if (["PROTECTION_ERROR", "UNKNOWN", "REJECTED", "FAILED", "CLOSING", "CANCELLED", "EXPIRED", "LIQUIDATED", "SUSPENDED", "DEGRADED", "DISCONNECTED", "UNAVAILABLE", "ERROR"].includes(value)) return "bad";
    return "muted";
  };

  const protectionTone = (state) => {
    const value = String(state || "").toUpperCase();
    if (["ACTIVE", "CONFIRMED", "ESTABLISHED"].includes(value)) return "good";
    if (["NOT_ESTABLISHED", "PENDING", "UNKNOWN"].includes(value)) return "warn";
    return "bad";
  };

  /* ------------------------------------------------------------------ persistence -- */
  const prefs = {
    load() {
      try { return JSON.parse(localStorage.getItem(PREF_STORAGE_KEY) || "{}") || {}; }
      catch { return {}; }
    },
    save(patch) {
      try { localStorage.setItem(PREF_STORAGE_KEY, JSON.stringify({ ...prefs.load(), ...patch })); }
      catch { /* private mode: preferences are optional */ }
    },
  };

  /* --------------------------------------------------------------------- state ----- */
  const state = {
    authMethod: null,
    sessionToken: localStorage.getItem(SESSION_STORAGE_KEY) || "",
    serverToken: localStorage.getItem(TOKEN_STORAGE_KEY) || "",
    user: null,
    subscription: null,
    disclaimer: null,
    permissions: { can_manage_users: false, can_change_settings: false, can_trade: false },
    settings: null,
    overview: null,
    positions: [],
    pendingEntries: [],
    events: [],
    history: [],
    market: null,
    health: null,
    performance: { series: [], mode: "PAPER", days: 30 },
    users: [],
    view: prefs.load().view || "dashboard",
    performanceDays: prefs.load().performanceDays || 30,
    historyMode: prefs.load().historyMode || "PAPER",
    historyOffset: 0,
    historyLimit: 25,
    signalsOffset: 0,
    signalsLimit: 25,
    signalStatus: "",
    positionFilter: "all",
    positionSort: "created_at",
    positionOrder: "desc",
    marketQuery: "",
    marketSort: "change_24h_percent",
    marketOrder: "desc",
    liveFeed: false,
    polling: null,
    online: true,
    lastUpdate: null,
    firstPaint: true,
    knownPositionIds: new Set(),
  };

  /* --------------------------------------------------------------------- toasts ---- */
  const toast = (message, tone = "info") => {
    const stack = $("#toast-stack");
    if (!stack) return;
    const node = document.createElement("div");
    node.className = `toast ${tone}`;
    node.innerHTML = `<strong>${tone === "error" ? "Request failed" : tone === "success" ? "Done" : "Notice"}</strong><div>${esc(message)}</div>`;
    stack.appendChild(node);
    setTimeout(() => { node.style.opacity = "0"; setTimeout(() => node.remove(), 400); }, 6200);
  };

  const describeError = (error) => {
    if (!error) return "Unknown error";
    if (error.detail) return typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail);
    return error.message || String(error);
  };

  /* --------------------------------------------------------------------- api ------- */
  async function api(path, options = {}) {
    const { method = "GET", body, silent = false, raw = false } = options;
    const headers = { Accept: raw ? "*/*" : "application/json" };
    if (state.sessionToken) headers["X-Session-Token"] = state.sessionToken;
    if (state.serverToken) headers["X-Dashboard-Token"] = state.serverToken;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch (error) {
      state.online = false;
      renderConnectivity();
      if (!silent) toast("Network request failed. The console will retry automatically.", "error");
      throw error;
    }
    state.online = true;
    if (response.status === 401) {
      clearCredentials();
      showAuth("Your session expired. Sign in again.");
      throw new Error("Session expired");
    }
    let payload = null;
    const text = await response.text();
    if (text) { try { payload = JSON.parse(text); } catch { payload = text; } }
    if (!response.ok) {
      const detail = payload && typeof payload === "object" && payload.detail ? payload.detail : payload;
      const error = new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
      error.detail = detail;
      error.status = response.status;
      if (!silent) toast(describeError(error), "error");
      throw error;
    }
    return payload;
  }

  const clearCredentials = () => {
    state.sessionToken = "";
    state.serverToken = "";
    state.user = null;
    localStorage.removeItem(SESSION_STORAGE_KEY);
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  };

  /* --------------------------------------------------------------------- auth ------ */
  function showAuth(message = "") {
    $("#auth-overlay").classList.remove("hidden");
    $("#app-shell").hidden = true;
    const error = $("#auth-error");
    if (error) error.textContent = message;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    stopPolling();
  }

  function hideAuth() {
    $("#auth-overlay").classList.add("hidden");
    $("#app-shell").hidden = false;
  }

  async function loadPublicConfig() {
    try {
      const config = await api("/api/public/config", { silent: true });
      $("#auth-disclaimer-copy").textContent = config.disclaimer || "";
      $("#auth-fee").textContent = config.subscription
        ? `${config.subscription.fee ?? "—"} ${config.subscription.currency ?? ""} · ${config.subscription.billing_cycle ?? ""}`
        : "Unavailable";
      if (!config.server_token_signin_available) {
        $("#auth-token-fields")?.classList.add("hidden");
        $('.auth-tab[data-auth-mode="token"]')?.classList.add("hidden");
      }
      if (!config.disclaimer_required) $("#auth-disclaimer-accept").checked = true;
      return config;
    } catch {
      return null;
    }
  }

  async function signIn(mode, payload) {
    const button = $("#auth-submit");
    const errorBox = $("#auth-error");
    errorBox.textContent = "";
    button.disabled = true;
    button.innerHTML = `<span class="spinner"></span>Verifying`;
    try {
      if (mode === "password") {
        const result = await api("/api/auth/login", { method: "POST", body: payload, silent: true });
        state.sessionToken = result.session_token || "";
        state.user = result.user || null;
        state.subscription = result.subscription || null;
        state.authMethod = "SESSION";
        localStorage.setItem(SESSION_STORAGE_KEY, state.sessionToken);
        localStorage.removeItem(TOKEN_STORAGE_KEY);
        if (result.disclaimer) state.disclaimer = result.disclaimer;
        await loadIdentity();
        if (state.disclaimer && state.disclaimer.required && !state.disclaimer.accepted_at) {
          try {
            await api("/api/auth/disclaimer", { method: "POST", silent: true });
            await loadIdentity();
          } catch (error) {
            errorBox.textContent = `Signed in, but the disclaimer must be acknowledged: ${describeError(error)}`;
          }
        }
      } else {
        state.serverToken = String(payload || "").trim();
        if (!state.serverToken) throw new Error("Enter the dashboard access token");
        localStorage.setItem(TOKEN_STORAGE_KEY, state.serverToken);
        localStorage.removeItem(SESSION_STORAGE_KEY);
        state.sessionToken = "";
        state.authMethod = "SERVER_TOKEN";
        await loadIdentity();
      }
      hideAuth();
      await bootstrapDashboard();
      startPolling();
      toast("Console connected.", "success");
    } catch (error) {
      clearCredentials();
      errorBox.textContent = describeError(error);
    } finally {
      button.disabled = false;
      button.textContent = "Open console →";
    }
  }

  async function loadIdentity() {
    const identity = await api("/api/auth/me", { silent: true });
    state.authMethod = identity.auth_method || state.authMethod;
    state.user = identity.user || null;
    state.subscription = identity.subscription || state.subscription;
    state.disclaimer = identity.disclaimer || state.disclaimer;
    state.permissions = identity.permissions || state.permissions;
  }

  async function signOut() {
    try { await api("/api/auth/logout", { method: "POST", silent: true }); } catch { /* best effort */ }
    clearCredentials();
    state.user = null;
    state.subscription = null;
    stopPolling();
    showAuth("Signed out.");
  }

  /* ------------------------------------------------------------------- nav/views --- */
  const VIEW_TITLES = {
    dashboard: "Overview", positions: "Positions", markets: "Markets", signals: "Signal log",
    history: "Trade history", strategy: "Playbook", account: "Account", settings: "Settings", users: "Access control",
  };

  function setView(view) {
    if (!VIEW_TITLES[view]) view = "dashboard";
    state.view = view;
    prefs.save({ view });
    $$(".page").forEach((page) => page.classList.toggle("active", page.id === `view-${view}`));
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    const crumb = $("#page-crumb");
    if (crumb) crumb.textContent = VIEW_TITLES[view];
    if (window.innerWidth <= 860) $("#sidebar").classList.remove("open");
    if (view === "users") renderUsers();
    if (view === "markets" && !state.market) refreshMarkets();
  }

  /* ------------------------------------------------------------------ dashboard ---- */
  function renderDashboard() {
    const overview = state.overview;
    const settings = state.settings;
    if (!overview) return;
    const day = overview.day_pnl || {};
    const realized = toNumber(day.realized_net);
    const pnlNode = $("#kpi-day-pnl");
    if (pnlNode) {
      const text = realized === null ? "Unavailable" : `${realized > 0 ? "+" : ""}${realized.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      pnlNode.textContent = text;
      pnlNode.classList.toggle("positive", realized !== null && realized > 0);
      pnlNode.classList.toggle("negative", realized !== null && realized < 0);
    }
    const noteNode = $("#kpi-day-pnl-note");
    if (noteNode) {
      noteNode.textContent = day.unrealized_status === "AVAILABLE"
        ? `Unrealized ${fmtMoney(day.unrealized, day.currency, true)} · ${day.fees_included ? "fees included" : "fees excluded"}`
        : `${esc(overview.market_day || "")} market day · unrealized Unavailable (stale market data)`;
    }

    $("#kpi-open").textContent = String(overview.open_positions ?? 0);
    $("#kpi-open-note").textContent = overview.open_positions
      ? `${overview.pending_entries || 0} pending · ${overview.unknown_or_attention || 0} need attention`
      : "No confirmed open exposure";
    $("#kpi-signals").textContent = String(overview.signals_today ?? 0);
    $("#kpi-signal-note").textContent = `${overview.trades_today ?? 0} trades opened · ${overview.closed_today ?? 0} closed`;
    const closed = (overview.wins_today || 0) + (overview.losses_today || 0);
    const winRate = closed ? Math.round(((overview.wins_today || 0) / closed) * 100) : null;
    const winNode = $("#kpi-win-rate");
    winNode.textContent = winRate === null ? "Unavailable" : `${winRate}%`;
    $("#kpi-win-note").textContent = closed ? `${overview.wins_today}W / ${overview.losses_today}L of ${closed}` : "No closed trades today";
    const arc = $("#kpi-win-arc");
    if (arc) arc.style.strokeDashoffset = String(94.2 - (94.2 * (winRate ?? 0)) / 100);

    const counts = overview.status_counts || {};
    if ($("#nav-open-count")) $("#nav-open-count").textContent = String(overview.open_positions ?? 0);
    if ($("#nav-signal-count")) $("#nav-signal-count").textContent = String(overview.signals_today ?? 0);

    $("#positions-count").textContent = `${overview.open_positions ?? 0} open`;
    $("#positions-footer").textContent = overview.pending_entries
      ? `${overview.pending_entries} pending entr${overview.pending_entries === 1 ? "y" : "ies"} are not confirmed positions`
      : "Every value comes from stored trade state";

    renderDashboardPositions();
    renderDashboardSignals();
    renderStrategySnapshot(settings);
    renderSubscriptionPanel();
    renderSystemStatus();
    renderSystemAlert(counts);
    if ($("#last-updated")) $("#last-updated").textContent = fmtDateTime(state.lastUpdate);
    animateSchema();
  }

  function renderSystemAlert(counts) {
    const attention = (counts.PROTECTION_ERROR || 0) + (counts.UNKNOWN || 0);
    const banner = $("#system-alert");
    if (!attention) { banner.classList.add("hidden"); return; }
    banner.classList.remove("hidden");
    $("#system-alert-title").textContent = `${attention} trade${attention === 1 ? "" : "s"} need reconciliation`;
    $("#system-alert-copy").textContent = "Protection state is unresolved for at least one position. Open the positions view and reconcile before opening new risk.";
  }

  function renderConnectivity() {
    const dot = $("#sidebar-dot");
    dot.className = `status-dot ${state.online ? "ok" : "bad"}`;
    $("#sidebar-health").textContent = state.online ? "Connected" : "Offline";
    const health = state.health;
    const market = health?.components?.market_data;
    const label = market ? market.replaceAll("_", " ") : "Unavailable";
    $("#sidebar-market").textContent = `Market data: ${label}`;
    const worker = health?.components?.worker;
    $("#sidebar-latency").textContent = worker ? `Worker: ${worker} · outbox pending ${health?.outbox?.pending ?? "Unavailable"}` : "Worker status Unavailable";
    if ($("#nav-market-pulse")) $("#nav-market-pulse").style.background = market === "CONNECTED" ? "var(--green)" : market === "REST_FALLBACK" ? "var(--amber)" : "var(--red)";
  }

  function renderDashboardPositions() {
    const body = $("#dashboard-position-rows");
    const items = state.positions.slice(0, 5);
    if (!items.length) { body.innerHTML = `<tr><td colspan="7" class="empty-cell">No open positions. New signals will appear in the signal log.</td></tr>`; return; }
    body.innerHTML = items.map((item) => `
      <tr class="${state.knownPositionIds.has(item.id) ? "" : "new-row"}">
        <td><div class="contract-cell">${coinIcon(item.symbol)}<div><strong>${esc(item.symbol)}</strong><small>${esc(item.mode)} · ${esc(item.direction || "LONG")}</small></div></div></td>
        <td>${protectionRail(item)}</td>
        <td><div class="contract-cell"><div><strong>${fmtPrice(item.actual_entry || item.planned_entry)}</strong><small>last ${item.price_stale ? "stale / Unavailable" : fmtPrice(item.last_price)}</small></div></div></td>
        <td class="${pnlClass(item.unrealized_pnl)}">${item.unrealized_pnl === null ? "Unavailable" : fmtMoney(item.unrealized_pnl, "USDT", true)}</td>
        <td>${trailingCell(item)}</td>
        <td>${pill(item.status, statusTone(item.status))}</td>
        <td><button class="icon-button tiny" data-action="details" data-id="${esc(item.id)}" title="Trade details">···</button></td>
      </tr>`).join("");
    state.positions.forEach((item) => state.knownPositionIds.add(item.id));
  }

  function renderDashboardSignals() {
    const body = $("#dashboard-signal-rows");
    const items = state.events.slice(0, 6);
    if (!items.length) { body.innerHTML = `<tr><td colspan="7" class="empty-cell">No TradingView deliveries recorded yet.</td></tr>`; return; }
    body.innerHTML = items.map((event) => `
      <tr>
        <td class="muted">${esc(fmtClock(event.received_at))}</td>
        <td><div class="contract-cell"><div><strong>${esc(event.tradingview_symbol || "Unavailable")}</strong><small>Shark ${esc(event.symbol || "unresolved")}</small></div></div></td>
        <td>${esc(event.timeframe || "—")}</td>
        <td class="mono">${fmtPrice(event.signal_close)}</td>
        <td class="mono">${fmtPrice(event.entry_price)}</td>
        <td>${pill(event.status, statusTone(event.status))}</td>
        <td class="muted">${esc((event.status_reason || "").slice(0, 60) || "—")}</td>
      </tr>`).join("");
  }

  function protectionRail(item) {
    const entry = toNumber(item.actual_entry || item.planned_entry);
    const stop = toNumber(item.stop_price || item.initial_stop_price);
    const target = toNumber(item.target1_price);
    const last = toNumber(item.last_price);
    const values = [entry, stop, target, last].filter((value) => value !== null);
    if (values.length < 2) return `<span class="muted">Levels ${esc("Unavailable")}</span>`;
    const low = Math.min(...values);
    const high = Math.max(...values);
    const span = high - low || 1;
    const pos = (value) => value === null ? null : `${(8 + ((value - low) / span) * 84).toFixed(2)}%`;
    return `
      <div class="rail">
        <div class="rail-track">
          ${stop === null ? "" : `<span class="rail-stop" style="left:${pos(stop)}" title="Protective stop"></span>`}
          ${entry === null ? "" : `<span class="rail-entry" style="left:${pos(entry)}" title="Entry"></span>`}
          ${target === null ? "" : `<span class="rail-target" style="left:${pos(target)}" title="Target 1"></span>`}
          ${last === null ? "" : `<span class="rail-price" style="left:${pos(last)}" title="Last price"></span>`}
        </div>
        <div class="rail-labels"><span>SL ${fmtPrice(stop)}</span><span>TP1 ${fmtPrice(target)}</span></div>
      </div>`;
  }

  function trailingCell(item) {
    if (!item.trailing_enabled) return `<span class="muted">Off</span>`;
    if (isMissing(item.trail_high_water_price)) return `<span class="muted">Armed</span>`;
    return `<span class="mono">${fmtPrice(item.trail_high_water_price)}</span><small class="muted"> high water</small>`;
  }

  function renderStrategySnapshot(settings) {
    const list = $("#strategy-snapshot");
    if (!settings) { list.innerHTML = `<li class="muted">Strategy settings Unavailable</li>`; return; }
    const strategy = settings.strategy || {};
    const risk = settings.risk || {};
    list.innerHTML = [
      `<li><b>${esc(strategy.entry_candle_interval || "5m")}</b> Shark closed-candle entry price</li>`,
      `<li><b>-${esc(strategy.stop_loss_percent)}%</b> protective stop from entry</li>`,
      `<li><b>+${esc(strategy.target1_percent)}%</b> closes <b>${esc(strategy.target1_close_percent)}%</b> of the position</li>`,
      `<li><b>${strategy.trailing_enabled ? `${esc(strategy.trailing_stop_percent)}% trailing` : "Trailing disabled"}</b> after break-even</li>`,
      `<li>Margin <b>${fmtMoney(risk.margin_per_trade)}</b> at <b>${esc(risk.leverage)}×</b> · max ${esc(risk.max_open_positions)} open</li>`,
    ].join("");
    ["schema-interval", "flow-interval", "auth-interval"].forEach((id) => { const node = $(`#${id}`); if (node) node.textContent = strategy.entry_candle_interval || "5m"; });
    ["schema-levels", "flow-stop"].forEach((id) => { const node = $(`#${id}`); if (node) node.textContent = `-${strategy.stop_loss_percent}% stop · +${strategy.target1_percent}% target 1`; });
    if ($("#flow-target")) $("#flow-target").textContent = `+${strategy.target1_percent}%`;
    if ($("#flow-partial")) $("#flow-partial").textContent = `${strategy.target1_close_percent}%`;
    if ($("#flow-trail")) $("#flow-trail").textContent = `${strategy.trailing_stop_percent}%`;
    if ($("#flow-trail-2")) $("#flow-trail-2").textContent = `${strategy.trailing_stop_percent}%`;
    if ($("#schema-partial")) $("#schema-partial").textContent = `Close ${strategy.target1_close_percent}% at +${strategy.target1_percent}%`;
    if ($("#schema-trail")) $("#schema-trail").textContent = `Stop to entry, then ${strategy.trailing_stop_percent}% trail`;
    if ($("#auth-stop")) $("#auth-stop").textContent = `${strategy.stop_loss_percent}%`;
    if ($("#auth-trail")) $("#auth-trail").textContent = `${strategy.trailing_stop_percent}%`;
    if ($("#auth-partial")) $("#auth-partial").textContent = `${strategy.target1_close_percent}%`;
    if ($("#strategy-rr")) $("#strategy-rr").textContent = `R:R ${settings.risk_reward_ratio ?? "Unavailable"}`;
    if ($("#protection-ladder")) {
      $("#protection-ladder").innerHTML = [
        `<li><b>Entry</b> — last completed ${esc(strategy.entry_candle_interval || "5m")} Shark candle close</li>`,
        `<li><b>-${esc(strategy.stop_loss_percent)}%</b> — initial reduce-only stop with the entry</li>`,
        `<li><b>+${esc(strategy.target1_percent)}%</b> — close ${esc(strategy.target1_close_percent)}% and arm break-even</li>`,
        `<li><b>Break-even</b> — stop moves to the entry price</li>`,
        `<li><b>Trail</b> — each ${esc(strategy.trailing_stop_percent)}% advance lifts the stop ${esc(strategy.trailing_stop_percent)}%</li>`,
      ].join("");
    }
    if ($("#entry-provenance")) {
      $("#entry-provenance").innerHTML = [
        `<li><b>${esc(strategy.entry_price_source || "SHARK_CLOSED_CANDLE")}</b> — configured entry source</li>`,
        `<li>Candle age limit <b>${esc(strategy.entry_candle_max_age_seconds ?? "Unavailable")}s</b></li>`,
        `<li>Live ticker fallback <b>${strategy.entry_allow_live_ticker_fallback ? "enabled" : "disabled"}</b></li>`,
        `<li>Secondary fixed targets <b>${strategy.enable_secondary_targets ? "enabled" : "disabled"}</b></li>`,
      ].join("");
    }
    if ($("#schema-status")) $("#schema-status").textContent = settings.effective_mode === "LIVE" ? "LIVE mode is active: protection changes require exchange confirmation." : "Paper mode: fills are simulated from Shark public prices.";
  }

  function renderSubscriptionPanel() {
    const service = state.subscription || {};
    const badge = $("#subscription-status-badge");
    badge.textContent = service.status || "Unavailable";
    badge.className = `badge ${service.status === "ACTIVE" ? "badge-green" : service.status === "EXPIRED" ? "badge-red" : "badge-amber"}`;
    $("#subscription-plan").textContent = service.plan_name || "Unavailable";
    $("#subscription-fee").textContent = service.fee === null || service.fee === undefined
      ? "Fee Unavailable"
      : `${service.fee} ${service.currency || ""} · ${service.billing_cycle || ""}`.trim();
    const days = service.days_remaining;
    $("#subscription-days").textContent = days === null || days === undefined ? "Renewal Unavailable" : `${days} day${days === 1 ? "" : "s"} remaining`;
    $("#subscription-expiry").textContent = service.expires_at ? `Expires ${fmtDateTime(service.expires_at)}` : "No expiry recorded";
    const bar = $("#subscription-progress-bar");
    const total = 30;
    bar.style.width = days === null || days === undefined ? "0%" : `${Math.max(0, Math.min(100, (days / total) * 100))}%`;
    if ($("#nav-sub-days")) $("#nav-sub-days").textContent = days === null || days === undefined ? "—" : `${days}d`;
  }

  function renderSystemStatus() {
    const health = state.health;
    const overall = $("#system-overall");
    if (!health) { overall.innerHTML = `<span class="system-orbit"><i></i></span><div><strong>Status Unavailable</strong><small>The health endpoint has not answered yet</small></div>`; return; }
    const degraded = health.status !== "OK";
    overall.innerHTML = `
      <span class="system-orbit"><i style="background:${degraded ? "var(--amber)" : "var(--green)"}"></i></span>
      <div><strong>${esc(health.status === "OK" ? "All core services reporting" : "Degraded — check components")}</strong>
      <small>${esc(health.market_data_note || "Health is based on observed service state")}</small></div>`;
    const components = health.components || {};
    $("#service-list").innerHTML = Object.entries(components).map(([name, value]) => `
      <div class="service-row"><span class="status-dot ${statusTone(value) === "good" ? "ok" : statusTone(value) === "warn" ? "warn" : "bad"}"></span>
      <span>${esc(name.replaceAll("_", " "))}</span><b>${esc(String(value ?? "Unavailable"))}</b></div>`).join("");
    $("#health-checked-at").textContent = fmtDateTime(health.timestamp);
    $("#clock-dot").className = `status-dot ${degraded ? "warn" : "ok"}`;
  }

  function animateSchema() {
    const packet = $("#schema-packet");
    if (!packet) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const combined = ["#schema-path-1", "#schema-path-2", "#schema-path-3"]
      .map((selector) => $(selector))
      .filter(Boolean);
    if (!combined.length) return;
    const total = combined.reduce((sum, path) => sum + path.getTotalLength(), 0);
    if (reduced) {
      const point = combined[0].getPointAtLength(combined[0].getTotalLength() / 2);
      packet.setAttribute("cx", point.x); packet.setAttribute("cy", point.y);
      return;
    }
    let travelled = 0;
    const step = () => {
      if (!document.body.contains(packet)) return;
      travelled = (travelled + 7) % total;
      let remaining = travelled;
      for (const path of combined) {
        const length = path.getTotalLength();
        if (remaining <= length) {
          const point = path.getPointAtLength(remaining);
          packet.setAttribute("cx", point.x);
          packet.setAttribute("cy", point.y);
          break;
        }
        remaining -= length;
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  /* ------------------------------------------------------------------- positions --- */
  const ACTIVE_STATUSES = new Set(["OPEN", "PARTIALLY_CLOSED", "ENTRY_PARTIAL", "PROTECTION_ERROR", "UNKNOWN", "CLOSING", "ENTRY_PENDING", "SUBMITTING", "QUEUED"]);

  function getPositionSortVal(item, key) {
    switch (key) {
      case "symbol":
        return item.symbol || "";
      case "rail": {
        const entry = toNumber(item.actual_entry || item.planned_entry) || 0;
        const stop = toNumber(item.stop_price || item.initial_stop_price) || 0;
        return entry && stop ? ((entry - stop) / entry) : 0;
      }
      case "entry":
        return toNumber(item.actual_entry || item.planned_entry) || 0;
      case "size":
        return toNumber(item.remaining_quantity || item.quantity) || 0;
      case "leverage":
        return toNumber(item.leverage) || 0;
      case "realized":
        return toNumber(item.net_realized_pnl ?? item.realized_pnl) || 0;
      case "unrealized":
        return item.unrealized_pnl !== null ? toNumber(item.unrealized_pnl) : -Infinity;
      case "net_profit": {
        const real = toNumber(item.net_realized_pnl ?? item.realized_pnl) || 0;
        const unreal = toNumber(item.unrealized_pnl) || 0;
        return toNumber(item.net_profit_pnl) !== null ? toNumber(item.net_profit_pnl) : (real + unreal);
      }
      case "trail":
        return toNumber(item.trail_high_water_price) || (item.trailing_active ? 1 : 0);
      case "protection":
        return item.protection_state || "";
      case "status":
        return item.status || "";
      default:
        return item.created_at || "";
    }
  }

  function renderPositions() {
    const body = $("#position-rows");
    const filter = state.positionFilter;
    let items = state.positions.filter((item) => filter === "all" ? true : filter === "ENTRY_PENDING" ? ["ENTRY_PENDING", "SUBMITTING", "QUEUED"].includes(item.status) : item.status === filter);

    // Compute live positions summary KPIs
    let totalRealized = 0;
    let totalUnrealized = 0;
    let totalMargin = 0;
    let openCount = 0;

    state.positions.forEach((item) => {
      const real = toNumber(item.net_realized_pnl ?? item.realized_pnl) || 0;
      const unreal = toNumber(item.unrealized_pnl) || 0;
      const margin = toNumber(item.margin) || 0;
      totalRealized += real;
      totalUnrealized += unreal;
      totalMargin += margin;
      if (["OPEN", "PARTIALLY_CLOSED", "ENTRY_PARTIAL"].includes(item.status)) {
        openCount++;
      }
    });
    const totalNetProfit = totalRealized + totalUnrealized;

    if ($("#pos-kpi-realized")) {
      $("#pos-kpi-realized").textContent = fmtMoney(totalRealized, "USDT", true);
      $("#pos-kpi-realized").className = `kpi-value ${pnlClass(totalRealized)}`;
    }
    if ($("#pos-kpi-unrealized")) {
      $("#pos-kpi-unrealized").textContent = fmtMoney(totalUnrealized, "USDT", true);
      $("#pos-kpi-unrealized").className = `kpi-value ${pnlClass(totalUnrealized)}`;
    }
    if ($("#pos-kpi-net")) {
      $("#pos-kpi-net").textContent = fmtMoney(totalNetProfit, "USDT", true);
      $("#pos-kpi-net").className = `kpi-value ${pnlClass(totalNetProfit)}`;
    }
    if ($("#pos-kpi-margin")) {
      $("#pos-kpi-margin").textContent = fmtMoney(totalMargin, "USDT");
      const note = $("#pos-kpi-count-note");
      if (note) note.textContent = `${openCount} active position${openCount === 1 ? "" : "s"}`;
    }

    if (state.positionSort) {
      items.sort((a, b) => {
        let va = getPositionSortVal(a, state.positionSort);
        let vb = getPositionSortVal(b, state.positionSort);

        if (typeof va === "string" || typeof vb === "string") {
          va = String(va).toLowerCase();
          vb = String(vb).toLowerCase();
          return state.positionOrder === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
        }

        if (va === null || va === undefined || isNaN(va)) va = -Infinity;
        if (vb === null || vb === undefined || isNaN(vb)) vb = -Infinity;

        return state.positionOrder === "asc" ? (va - vb) : (vb - va);
      });
    }

    $$("#view-positions table thead th[data-sort]").forEach((th) => {
      const isSorted = th.dataset.sort === state.positionSort;
      th.classList.toggle("sorted-asc", isSorted && state.positionOrder === "asc");
      th.classList.toggle("sorted-desc", isSorted && state.positionOrder === "desc");
      const icon = th.querySelector(".sort-icon");
      if (icon) {
        icon.textContent = isSorted ? (state.positionOrder === "asc" ? "▲" : "▼") : "⇅";
      }
    });

    if (!items.length) {
      body.innerHTML = `<tr><td colspan="12" class="empty-cell">No positions match this filter.</td></tr>`;
    } else {
      body.innerHTML = items.map((item) => {
        const percentage = (item.stop_price && item.actual_entry && toNumber(item.actual_entry))
          ? (((toNumber(item.stop_price) - toNumber(item.actual_entry)) / toNumber(item.actual_entry)) * 100).toFixed(2) : null;
        
        const realizedVal = toNumber(item.net_realized_pnl ?? item.realized_pnl) ?? 0;
        const unrealizedVal = item.unrealized_pnl !== null ? toNumber(item.unrealized_pnl) : null;
        const netProfitVal = toNumber(item.net_profit_pnl) !== null ? toNumber(item.net_profit_pnl) : (realizedVal + (unrealizedVal || 0));
        const netRoi = (item.margin && toNumber(item.margin) > 0) ? ((netProfitVal / toNumber(item.margin)) * 100).toFixed(2) : null;

        return `
        <tr>
          <td><div class="contract-cell">${coinIcon(item.symbol)}<div><strong>${esc(item.symbol)}</strong><small>${esc(item.tradingview_symbol || "")} · ${esc(item.mode)}</small></div></div></td>
          <td>${protectionRail(item)}</td>
          <td><div class="contract-cell"><div><strong>${fmtPrice(item.actual_entry || item.planned_entry)}</strong><small>${item.price_stale ? "stale / Unavailable" : `last ${fmtPrice(item.last_price)}`}</small></div></div></td>
          <td><div class="contract-cell"><div><strong>${fmtNum(item.remaining_quantity)}</strong><small>of ${fmtNum(item.quantity)}</small></div></div></td>
          <td>${esc(item.leverage)}×</td>
          <td class="${pnlClass(realizedVal)}">${fmtMoney(realizedVal, "USDT", true)}</td>
          <td class="${pnlClass(unrealizedVal)}">
            <div>
              <strong>${unrealizedVal === null ? "Unavailable" : fmtMoney(unrealizedVal, "USDT", true)}</strong>
              ${item.roi_percent_on_margin !== null ? `<small class="muted">${fmtNum(item.roi_percent_on_margin, 2)}%</small>` : ""}
            </div>
          </td>
          <td class="${pnlClass(netProfitVal)}">
            <div class="net-profit-cell">
              <strong class="highlight-pnl">${fmtMoney(netProfitVal, "USDT", true)}</strong>
              ${netRoi !== null ? `<small class="muted">${netRoi}% net</small>` : ""}
            </div>
          </td>
          <td>${trailingCell(item)}</td>
          <td><div class="contract-cell"><div>${pill(item.protection_state, protectionTone(item.protection_state))}<small>${percentage === null ? "" : `${percentage}% from entry · `}${esc(item.stop_stage || "")}</small></div></div></td>
          <td>${pill(item.status, statusTone(item.status))}${item.status_reason ? `<small class="muted">${esc(item.status_reason.slice(0, 40))}</small>` : ""}</td>
          <td><div class="inline-actions">
            <button class="button button-secondary button-small" data-action="details" data-id="${esc(item.id)}">Details</button>
            ${["OPEN", "PARTIALLY_CLOSED", "PROTECTION_ERROR", "UNKNOWN"].includes(item.status) ? `<button class="button button-danger button-small" data-action="close" data-id="${esc(item.id)}" data-symbol="${esc(item.symbol)}">Close</button>` : ""}
          </div></td>
        </tr>`;
      }).join("");
    }
    renderPendingEntries();
    $("#positions-full-footer").textContent = `${state.positions.length} tracked position${state.positions.length === 1 ? "" : "s"} · ${state.pendingEntries.length} pending entr${state.pendingEntries.length === 1 ? "y" : "ies"}`;
  }

  function renderPendingEntries() {
    const body = $("#pending-rows");
    const items = state.pendingEntries;
    if (!items.length) { body.innerHTML = `<tr><td colspan="6" class="empty-cell">No pending entries. Limit orders that have not filled appear here.</td></tr>`; return; }
    body.innerHTML = items.map((item) => `
      <tr>
        <td><div class="contract-cell">${coinIcon(item.symbol)}<div><strong>${esc(item.symbol)}</strong><small>${esc(item.mode)} · ${esc(item.direction || "LONG")}</small></div></div></td>
        <td class="mono">${fmtPrice(item.planned_entry)}<small class="muted"> signal ${fmtPrice(item.signal_close)}</small></td>
        <td class="mono">${fmtNum(item.quantity)}</td>
        <td class="mono">${isMissing(item.entry_expires_at) ? "Unavailable" : fmtDateTime(item.entry_expires_at)}</td>
        <td>${pill(item.status, statusTone(item.status))}</td>
        <td><button class="button button-danger button-small" data-action="cancel-entry" data-id="${esc(item.id)}" data-symbol="${esc(item.symbol)}">Cancel entry</button></td>
      </tr>`).join("");
  }

  /* --------------------------------------------------------------------- markets --- */
  function horizontalRangeBar(lowVal, highVal, currentVal, changePct, isDay = false) {
    const low = toNumber(lowVal);
    const high = toNumber(highVal);
    const current = toNumber(currentVal);
    const pct = toNumber(changePct);

    if (low === null || high === null || current === null || low <= 0 || high <= 0) {
      return `
        <div class="range-cell">
          <div class="range-top">
            <span class="range-pct ${pnlClass(pct)}">${fmtPercent(pct)}</span>
            <span class="range-pos mono">—</span>
          </div>
          <div class="range-bottom mono"><span class="muted">Range Unavailable</span></div>
        </div>`;
    }

    const span = high - low;
    let pos = 50;
    if (span > 0) {
      pos = Math.min(100, Math.max(0, ((current - low) / span) * 100));
    } else if (current >= high) {
      pos = 100;
    } else if (current <= low) {
      pos = 0;
    }

    const fillClass = isDay ? "range-fill-day" : "range-fill-24h";
    const label = isDay ? "Day" : "24h";

    return `
      <div class="range-cell">
        <div class="range-top">
          <span class="range-pct ${pnlClass(pct)}">${fmtPercent(pct)}</span>
          <span class="range-pos mono" title="${label} range position">${pos.toFixed(1)}%</span>
        </div>
        <div class="range-bar" title="${label} Range: Low ${fmtPrice(low)} → High ${fmtPrice(high)} · Last: ${fmtPrice(current)}">
          <div class="range-track">
            <div class="range-fill ${fillClass}" style="width: ${pos.toFixed(2)}%"></div>
            <div class="range-pin" style="left: ${pos.toFixed(2)}%"></div>
          </div>
        </div>
        <div class="range-bottom mono">
          <span class="range-val-low" title="${label} Low">L ${fmtPrice(low)}</span>
          <span class="range-val-high" title="${label} High">H ${fmtPrice(high)}</span>
        </div>
      </div>
    `;
  }

  async function refreshMarkets() {
    const params = new URLSearchParams({ q: state.marketQuery, sort: state.marketSort, order: state.marketOrder });
    state.market = await api(`/api/market?${params}`, { silent: true });
    renderMarkets();
    if (state.liveFeed) await refreshLiveFeed();
  }

  async function refreshLiveFeed() {
    try {
      const feed = await api("/api/market/live-feed?limit=120", { silent: true });
      renderTape(feed);
    } catch { /* the tape is decorative; failures stay silent */ }
  }

  function renderMarkets() {
    const market = state.market;
    if (!market) return;
    const body = $("#market-rows");
    const items = market.items || [];
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty-cell">No public market data yet. The feed reports Unavailable until the first tick arrives.</td></tr>`;
    } else {
      body.innerHTML = items.slice(0, 300).map((item) => `
        <tr>
          <td><div class="contract-cell">${coinIcon(item.symbol)}<div><strong>${esc(item.symbol)}</strong><small>${esc(item.base_asset)} / ${esc(item.quote_asset)}</small></div></div></td>
          <td class="mono"><strong>${fmtPrice(item.last_price)}</strong></td>
          <td>${horizontalRangeBar(item.low_24h, item.high_24h, item.last_price, item.change_24h_percent, false)}</td>
          <td>${horizontalRangeBar(item.day_low, item.day_high, item.last_price, item.day_change_pct, true)}</td>
          <td class="mono">${fmtCompact(item.volume_24h)}</td>
          <td>${pill(item.source || "Unknown", "muted")}</td>
          <td class="muted">${fmtClock(item.updated_at)}</td>
        </tr>`).join("");
    }
    $("#market-footer").textContent = `${market.count ?? items.length} contract${(market.count ?? items.length) === 1 ? "" : "s"} · stale after ${market.stale_after_seconds ?? "?"}s`;
    $("#market-health").textContent = `Feed: ${esc((market.health || "Unavailable").replaceAll("_", " "))}`;
    const changes = items.map((item) => toNumber(item.day_change_pct)).filter((value) => value !== null);
    const positive = changes.filter((value) => value > 0).length;
    const tone = changes.length ? (positive / changes.length) : null;
    const label = tone === null ? "Unavailable" : tone > 0.65 ? "Risk-on" : tone < 0.35 ? "Risk-off" : "Mixed";
    $("#market-sentiment").textContent = label;
    $("#market-sentiment-note").textContent = changes.length ? `${positive} of ${changes.length} contracts positive today` : "Composite of tracked contracts";
    $("#nav-market-pulse").title = `Market day tone: ${label}`;
  }

  function renderTape(feed) {
    const tape = $("#market-tape");
    if (!feed || !feed.length) { tape.innerHTML = `<div class="muted">Tape Unavailable</div>`; return; }
    tape.innerHTML = feed.slice(0, 40).map((row) => `
      <div class="tape-row"><span class="status-dot ok"></span><span>${esc(row.symbol)}</span>
      <span class="${pnlClass(row.day_change_pct)}">${fmtPercent(row.day_change_pct)}</span>
      <strong>${fmtPrice(row.last_price)}</strong></div>`).join("");
  }

  async function recordSentiment() {
    const label = $("#market-sentiment")?.textContent;
    if (!label || label === "Unavailable") return;
    const previous = state.lastSentiment;
    state.lastSentiment = label;
    if (!previous || previous === label) return;
    try { await api("/api/market/sentiment-history", { method: "POST", body: { from: previous, to: label }, silent: true }); } catch { /* history is optional */ }
  }

  async function refreshSentimentHistory() {
    try {
      const rows = await api("/api/market/sentiment-history?limit=30", { silent: true });
      $("#sentiment-history").innerHTML = rows.length
        ? rows.map((row) => `<div class="tape-row"><span>${esc(row.from)}</span><span class="muted">→</span><span>${esc(row.to)}</span><strong class="muted">${fmtClock(row.time)}</strong></div>`).join("")
        : `<div class="muted">No recorded transitions yet.</div>`;
    } catch { $("#sentiment-history").innerHTML = `<div class="muted">History Unavailable</div>`; }
  }

  /* --------------------------------------------------------------------- signals --- */
  async function refreshSignals() {
    const params = new URLSearchParams({ limit: String(state.signalsLimit), offset: String(state.signalsOffset) });
    if (state.signalStatus) params.set("status", state.signalStatus);
    const data = await api(`/api/events?${params}`, { silent: true });
    state.events = data.items || [];
    state.eventsTotal = data.total ?? state.events.length;
    renderSignals();
    renderDashboardSignals();
  }

  function renderSignals() {
    const body = $("#signal-rows");
    if (!state.events.length) { body.innerHTML = `<tr><td colspan="10" class="empty-cell">No signal events for this filter.</td></tr>`; return; }
    body.innerHTML = state.events.map((event) => {
      const jobs = event.outbox || [];
      const failed = jobs.some((job) => job.status === "FAILED");
      const delivered = jobs.filter((job) => job.status === "DELIVERED").length;
      const notice = jobs.length ? `${delivered}/${jobs.length} delivered` : "No notification queued";
      return `
      <tr>
        <td class="muted">${fmtDateTime(event.received_at)}</td>
        <td><div class="contract-cell">${coinIcon(event.symbol || event.tradingview_symbol)}<div><strong>${esc(event.tradingview_symbol || "Unavailable")}</strong><small>Shark ${esc(event.symbol || "unresolved")}</small></div></div></td>
        <td>${pill(event.direction || "LONG", "muted")}</td>
        <td>${esc(event.timeframe || "—")}</td>
        <td class="mono">${fmtPrice(event.signal_close)}</td>
        <td class="mono">${fmtPrice(event.entry_price)}</td>
        <td>${pill(event.mode || "PAPER", "muted")}</td>
        <td>${pill(event.status, statusTone(event.status))}</td>
        <td class="muted">${esc((event.status_reason || "—").slice(0, 70))}</td>
        <td><div class="inline-actions">${failed ? `<button class="button button-secondary button-small" data-action="retry-notice" data-id="${esc(event.id)}">Retry</button>` : `<span class="muted">${esc(notice)}</span>`}</div></td>
      </tr>`;
    }).join("");
    const page = Math.floor(state.signalsOffset / state.signalsLimit) + 1;
    $("#signals-page").textContent = `Page ${page}`;
    $("#signals-footer").textContent = `${state.eventsTotal ?? state.events.length} events · showing ${state.events.length}`;
    $("#signals-prev").disabled = state.signalsOffset === 0;
    $("#signals-next").disabled = state.signalsOffset + state.signalsLimit >= (state.eventsTotal ?? 0);
  }

  /* --------------------------------------------------------------------- history --- */
  async function refreshHistory() {
    const params = new URLSearchParams({ limit: String(state.historyLimit), offset: String(state.historyOffset), mode: state.historyMode });
    const data = await api(`/api/history?${params}`, { silent: true });
    state.history = data.items || [];
    state.historyTotal = data.total ?? state.history.length;
    renderHistory();
  }

  function renderHistory() {
    const body = $("#history-rows");
    if (!state.history.length) { body.innerHTML = `<tr><td colspan="11" class="empty-cell">No trades recorded for ${esc(state.historyMode)} mode.</td></tr>`; }
    else {
      body.innerHTML = state.history.map((trade) => `
        <tr>
          <td class="muted">${fmtDateTime(trade.created_at || trade.opened_at)}</td>
          <td class="muted">${isMissing(trade.closed_at) ? "Open" : fmtDateTime(trade.closed_at)}</td>
          <td><div class="contract-cell">${coinIcon(trade.symbol)}<div><strong>${esc(trade.symbol)}</strong><small>${esc(trade.tradingview_symbol || "")}</small></div></div></td>
          <td class="mono">${fmtPrice(trade.actual_entry || trade.planned_entry)}</td>
          <td class="mono">${fmtPrice(trade.exit_price)}</td>
          <td class="mono">${fmtNum(trade.quantity)}</td>
          <td class="${pnlClass(trade.gross_realized_pnl)}">${trade.gross_realized_pnl === null ? "Unavailable" : fmtMoney(trade.gross_realized_pnl, "USDT", true)}</td>
          <td class="mono muted">${fmtMoney(trade.fees_paid)}</td>
          <td class="${pnlClass(trade.net_realized_pnl)}">${trade.net_realized_pnl === null ? "Unavailable" : fmtMoney(trade.net_realized_pnl, "USDT", true)}</td>
          <td>${pill(trade.exit_reason || trade.status, statusTone(trade.status))}</td>
          <td><button class="button button-secondary button-small" data-action="details" data-id="${esc(trade.id)}">Details</button></td>
        </tr>`).join("");
    }
    const page = Math.floor(state.historyOffset / state.historyLimit) + 1;
    $("#history-total").textContent = `${state.historyTotal ?? 0} ${state.historyMode} trades`;
    $("#history-page-label").textContent = `Page ${page} · showing ${state.history.length}`;
    $("#history-prev").disabled = state.historyOffset === 0;
    $("#history-next").disabled = state.historyOffset + state.historyLimit >= (state.historyTotal ?? 0);
    $("#csv-export").href = `/api/history.csv?mode=${encodeURIComponent(state.historyMode)}`;
  }

  /* -------------------------------------------------------------------- strategy --- */
  function renderStrategy() {
    const settings = state.settings;
    if (!settings) return;
    const risk = settings.risk || {};
    const strategy = settings.strategy || {};
    const form = $("#strategy-form");
    form.elements.entry_candle_interval.value = strategy.entry_candle_interval || risk.entry_candle_interval || "5m";
    form.elements.stop_loss_percent.value = strategy.stop_loss_percent ?? risk.stop_loss_percent ?? "";
    form.elements.target1_percent.value = strategy.target1_percent ?? risk.target1_percent ?? "";
    form.elements.target1_close_percent.value = strategy.target1_close_percent ?? risk.target1_close_percent ?? "";
    form.elements.trailing_stop_percent.value = strategy.trailing_stop_percent ?? risk.trailing_stop_percent ?? "";
    form.elements.trailing_enabled.checked = Boolean(strategy.trailing_enabled);
    form.elements.enable_secondary_targets.checked = Boolean(strategy.enable_secondary_targets);
    const entry = 100;
    const step = settings.risk?.price_tick ? Number(settings.risk.price_tick) : null;
    const stopPercent = toNumber(strategy.stop_loss_percent) ?? 2;
    const targetPercent = toNumber(strategy.target1_percent) ?? 2;
    const trailPercent = toNumber(strategy.trailing_stop_percent) ?? 1;
    const partialPercent = toNumber(strategy.target1_close_percent) ?? 50;
    $("#strategy-example").innerHTML = [
      ["Entry (example)", fmtPrice(entry)],
      ["Stop loss", fmtPrice(entry * (1 - stopPercent / 100))],
      ["Target 1", fmtPrice(entry * (1 + targetPercent / 100))],
      ["Closed at Target 1", `${partialPercent}% of size`],
      ["Break-even stop", fmtPrice(entry)],
      ["Trail after +3%", fmtPrice(entry * 1.03 * (1 - trailPercent / 100))],
    ].map(([label, value]) => `<div class="example-item"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("") + (step ? "" : "");
    renderStrategyAudit();
  }

  async function renderStrategyAudit() {
    try {
      const events = await api("/api/events?limit=8", { silent: true });
      const rows = (events.items || []).filter((event) => event.status_reason || event.status);
      $("#strategy-audit").innerHTML = rows.length
        ? rows.map((event) => `<div class="audit-item"><time>${fmtDateTime(event.received_at)}</time><span>${esc(event.symbol || event.tradingview_symbol || "—")} ${pill(event.status, statusTone(event.status))} ${esc((event.status_reason || "").slice(0, 60))}</span></div>`).join("")
        : `<div class="muted">No strategy activity recorded yet.</div>`;
    } catch { $("#strategy-audit").innerHTML = `<div class="muted">Audit trail Unavailable</div>`; }
  }

  async function saveStrategy(form) {
    const status = $("#strategy-save-status");
    status.textContent = "Saving…";
    status.className = "form-status";
    try {
      await api("/api/settings", {
        method: "PATCH",
        body: {
          entry_candle_interval: form.elements.entry_candle_interval.value.trim(),
          stop_loss_percent: form.elements.stop_loss_percent.value,
          target1_percent: form.elements.target1_percent.value,
          target1_close_percent: form.elements.target1_close_percent.value,
          trailing_stop_percent: form.elements.trailing_stop_percent.value,
          trailing_enabled: form.elements.trailing_enabled.checked,
          enable_secondary_targets: form.elements.enable_secondary_targets.checked,
        },
      });
      status.textContent = "Strategy rules saved.";
      status.className = "form-status ok";
      await refreshAll();
    } catch (error) {
      status.textContent = describeError(error);
      status.className = "form-status error";
    }
  }

  /* -------------------------------------------------------------------- account ---- */
  function renderAccount() {
    const user = state.user;
    const service = state.subscription || {};
    const userSub = state.userSubscription || null;
    $("#account-username").textContent = user ? user.username : "Server token";
    $("#account-display").textContent = user ? (user.display_name || "Operator account") : "Automation credential";
    $("#account-avatar").textContent = (user?.username || "SA").slice(0, 2).toUpperCase();
    $("#user-name").textContent = user ? (user.display_name || user.username) : "Automation token";
    $("#user-role").textContent = user ? `${user.role} · ${user.is_active ? "active" : "disabled"}` : "Server token access";
    $("#user-avatar").textContent = (user?.username || "SA").slice(0, 2).toUpperCase();
    const plan = (userSub && userSub.plan) || service.plan_name;
    const fee = (userSub && userSub.fee) || service.fee;
    const currency = (userSub && userSub.currency) || service.currency;
    const cycle = (userSub && userSub.cycle) || service.billing_cycle;
    const expires = (userSub && userSub.expires_at) || service.expires_at;
    const days = userSub ? userSub.days_remaining : service.days_remaining;
    const statusValue = (userSub && userSub.status) || service.status;
    $("#account-fee").textContent = fee === null || fee === undefined ? "Unavailable" : `${fee} ${currency || ""}`.trim();
    $("#account-cycle").textContent = `${cycle || "billing cycle Unavailable"}`;
    $("#account-plan").textContent = plan ? `Plan ${plan}` : "Plan Unavailable";
    $("#account-expiry").textContent = expires ? `Expires ${fmtDateTime(expires)}` : "No expiry recorded";
    $("#account-contact").textContent = service.contact ? `Billing contact ${service.contact}` : "Billing contact Unavailable";
    $("#account-days").textContent = days === null || days === undefined ? "—" : `${days}d`;
    const arc = $("#account-ring-arc");
    if (arc) arc.style.strokeDashoffset = String(326 - (326 * Math.max(0, Math.min(30, days ?? 0))) / 30);
    const badge = $("#account-sub-badge");
    badge.textContent = statusValue || "Unavailable";
    badge.className = `badge ${statusValue === "ACTIVE" ? "badge-green" : statusValue === "EXPIRED" ? "badge-red" : "badge-amber"}`;
    $("#subscription-enforcement").textContent = state.settings?.subscription?.enforced === false
      ? "Subscription state is recorded but not currently enforced on new signals."
      : "New signals are blocked while the plan is expired, suspended or cancelled.";
    $("#account-list").innerHTML = user ? [
      ["Username", user.username],
      ["Role", user.role],
      ["Account status", user.is_active ? "ACTIVE" : "DISABLED"],
      ["Last login", user.last_login_at ? fmtDateTime(user.last_login_at) : "Never"],
      ["Created", fmtDateTime(user.created_at)],
      ["Subscription source", "Per-account plan"],
    ].map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value ?? "Unavailable")}</dd></div>`).join("")
      : `<div><dt>Authentication</dt><dd>Server automation token</dd></div><div><dt>Account management</dt><dd>Not applicable</dd></div>`;
    const disclaimer = state.disclaimer || {};
    const stateBadge = $("#disclaimer-state");
    stateBadge.textContent = disclaimer.accepted_at ? "ACKNOWLEDGED" : "REQUIRED";
    stateBadge.className = `badge ${disclaimer.accepted_at ? "badge-green" : "badge-amber"}`;
    $("#disclaimer-copy").textContent = disclaimer.text || "Disclaimer text Unavailable";
    $("#accept-disclaimer").disabled = Boolean(disclaimer.accepted_at) || !state.user;
    $("#disclaimer-status").textContent = disclaimer.accepted_at ? `Accepted ${fmtDateTime(disclaimer.accepted_at)}` : "Acknowledgement required before order-changing calls";
    $("#renew-button").disabled = !state.user;
    $("#edit-subscription-button").disabled = !state.permissions.can_change_settings;
  }

  /* -------------------------------------------------------------------- settings --- */
  function renderSettings() {
    const settings = state.settings;
    if (!settings) return;
    const risk = settings.risk || {};
    const form = $("#risk-form");
    ["margin_per_trade", "leverage", "max_open_positions", "max_notional_per_trade", "target2_percent", "target3_percent", "max_daily_loss"].forEach((field) => {
      if (form.elements[field]) form.elements[field].value = risk[field] ?? "";
    });
    if (form.elements.limit_entry_expiry_seconds) form.elements.limit_entry_expiry_seconds.value = risk.limit_entry_expiry_seconds ?? 900;
    form.elements.cancel_stale_entry_orders.checked = Boolean(risk.cancel_stale_entry_orders);
    form.elements.allow_symbols.value = (risk.allow_symbols || []).join(", ");
    form.elements.deny_symbols.value = (risk.deny_symbols || []).join(", ");

    const mode = settings.effective_mode || settings.mode || "PAPER";
    const pillNode = $("#mode-pill");
    pillNode.className = `mode-pill ${mode === "LIVE" ? "live" : "paper"}`;
    pillNode.querySelector("span").textContent = mode === "LIVE" ? "LIVE MODE" : "PAPER MODE";
    const settingsPill = $("#settings-mode-pill");
    settingsPill.className = `mode-pill ${mode === "LIVE" ? "live" : "paper"}`;
    settingsPill.querySelector("span").textContent = mode === "LIVE" ? "LIVE MODE" : "PAPER MODE";

    const paused = Boolean(settings.trading_paused);
    $("#kill-switch-state").innerHTML = `<span class="status-dot ${paused ? "bad" : "ok"}"></span><strong>${paused ? "New signals are paused" : "Trading is enabled"}</strong>`;
    $("#pause-button").textContent = paused ? "Resume trading" : "Pause trading";
    $("#settings-pause-button").textContent = paused ? "Resume new trades" : "Pause new trades";
    $("#settings-pause-button").disabled = Boolean(settings.environment_pause);

    const verified = settings.live_contract_rules_verified && settings.live_split_protection_verified;
    $("#live-checks").innerHTML = [
      ["Server LIVE gate", settings.live_server_gate_configured],
      ["Contract rules verified", settings.live_contract_rules_verified],
      ["Split protection verified", settings.live_split_protection_verified],
      ["Shark credentials", settings.shark_credentials_configured],
      ["Live-safe contracts", settings.live_available],
    ].map(([label, pass]) => `<span class="check-chip ${pass ? "pass" : "fail"}">${pass ? "✓" : "×"} ${esc(label)}</span>`).join("");
    $("#live-blocker-copy").textContent = verified
      ? "Server gates report verified protection semantics. LIVE still requires typed confirmation and a fresh risk acknowledgement."
      : "LIVE is fail-closed: official Shark tick/quantity filters or split TP/SL replacement semantics remain unverified.";
    $("#enable-live-button").disabled = !settings.live_available;

    const authBadge = $("#auth-status-badge");
    authBadge.textContent = settings.dashboard_auth_enabled ? "ENABLED" : "DISABLED";
    authBadge.className = `badge ${settings.dashboard_auth_enabled ? "badge-green" : "badge-amber"}`;
    $("#auth-status-description").innerHTML = settings.dashboard_auth_enabled
      ? 'Authentication is <strong>ENABLED</strong>. The console accepts an operator session (<code>X-Session-Token</code>) or the server token (<code>X-Dashboard-Token</code>).'
      : 'Authentication is <strong>DISABLED</strong>. Every same-origin caller can reach the API — enable it before exposing this host.';
    $("#toggle-auth-btn").textContent = settings.dashboard_auth_enabled ? "Disable dashboard-auth" : "Enable dashboard-auth";
    $("#toggle-auth-btn").disabled = !state.permissions.can_change_settings;

    $("#config-service-list").innerHTML = [
      ["Webhook ingress", settings.webhook_configured],
      ["Telegram alerts", settings.telegram_configured],
      ["Shark private API", settings.shark_credentials_configured],
      ["Dashboard authentication", settings.dashboard_auth_enabled],
      ["LIVE server gate", settings.live_server_gate_configured],
    ].map(([label, ready]) => `<div class="service-row"><span class="status-dot ${ready ? "ok" : "warn"}"></span><span>${esc(label)}</span><b>${ready ? "CONFIGURED" : "NOT CONFIGURED"}</b></div>`).join("");
    $("#paper-assumption").innerHTML = `Simulated starting balance <strong>${fmtMoney(settings.paper_initial_balance)}</strong> · maker <strong>${esc(settings.paper_maker_fee_bps)} bps</strong> · taker <strong>${esc(settings.paper_taker_fee_bps)} bps</strong>`;
    $("#timezone-value").textContent = `${settings.market_day_timezone || "Unavailable"} · market day ${settings.market_day || ""}`.trim();
    if ($("#disclaimer-ticker-copy") && state.disclaimer?.text) $("#disclaimer-ticker-copy").textContent = state.disclaimer.text;
  }

  async function saveRisk(form) {
    const status = $("#risk-save-status");
    status.textContent = "Saving…";
    status.className = "form-status";
    try {
      const body = {};
      ["margin_per_trade", "leverage", "max_open_positions", "max_notional_per_trade", "target2_percent", "target3_percent", "max_daily_loss", "limit_entry_expiry_seconds"].forEach((field) => {
        if (form.elements[field] && form.elements[field].value !== "") body[field] = form.elements[field].value;
      });
      body.cancel_stale_entry_orders = form.elements.cancel_stale_entry_orders.checked;
      body.allow_symbols = form.elements.allow_symbols.value.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean);
      body.deny_symbols = form.elements.deny_symbols.value.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean);
      await api("/api/settings", { method: "PATCH", body });
      status.textContent = "Risk parameters saved.";
      status.className = "form-status ok";
      await refreshAll();
    } catch (error) {
      status.textContent = describeError(error);
      status.className = "form-status error";
    }
  }

  /* ----------------------------------------------------------------------- users --- */
  async function renderUsers() {
    if (!state.permissions.can_manage_users) {
      $("#user-rows").innerHTML = `<tr><td colspan="7" class="empty-cell">Admin role required to manage operators.</td></tr>`;
      $("#users-count-badge").textContent = "Restricted";
      return;
    }
    try {
      const data = await api("/api/users", { silent: true });
      state.users = data.items || [];
    } catch (error) {
      $("#user-rows").innerHTML = `<tr><td colspan="7" class="empty-cell">${esc(describeError(error))}</td></tr>`;
      return;
    }
    $("#users-count-badge").textContent = `${state.users.length} account${state.users.length === 1 ? "" : "s"}`;
    $("#users-footer").textContent = `${state.users.filter((user) => user.is_active).length} active · ${state.users.filter((user) => !user.is_active).length} disabled`;
    $("#user-rows").innerHTML = state.users.length ? state.users.map((user) => `
      <tr>
        <td><div class="contract-cell"><div class="avatar">${esc((user.username || "?").slice(0, 2).toUpperCase())}</div><div><strong>${esc(user.username)}</strong><small>${esc(user.display_name || "")}</small></div></div></td>
        <td>${pill(user.role, user.role === "OWNER" ? "good" : "muted")}</td>
        <td>${pill(user.is_active ? "ACTIVE" : "DISABLED", user.is_active ? "good" : "bad")}</td>
        <td>${esc(user.subscription_plan || "Unavailable")}${user.subscription_fee ? ` · ${esc(user.subscription_fee)} ${esc(user.subscription_currency || "")}` : ""}</td>
        <td class="muted">${user.subscription_expires_at ? fmtDateTime(user.subscription_expires_at) : "No expiry"}</td>
        <td class="muted">${user.last_login_at ? fmtDateTime(user.last_login_at) : "Never"}</td>
        <td><div class="inline-actions">
          <button class="button button-secondary button-small" data-action="edit-user" data-id="${esc(user.id)}" data-role="${esc(user.role)}" data-active="${user.is_active}" data-username="${esc(user.username)}">Edit</button>
        </div></td>
      </tr>`).join("") : `<tr><td colspan="7" class="empty-cell">No operator accounts yet. The first bootstrap account is created from the server environment.</td></tr>`;
  }

  /* ---------------------------------------------------------------------- drawer --- */
  async function openDrawer(tradeId) {
    const backdrop = $("#drawer-backdrop");
    backdrop.classList.remove("hidden");
    $("#drawer-title").textContent = "Loading…";
    $("#drawer-body").innerHTML = `<div class="loading-line"></div><div class="loading-line"></div>`;
    try {
      const [trade, orders, audit] = await Promise.all([
        api(`/api/trades/${encodeURIComponent(tradeId)}`, { silent: true }),
        api(`/api/trades/${encodeURIComponent(tradeId)}/orders`, { silent: true }),
        api(`/api/trades/${encodeURIComponent(tradeId)}/audit`, { silent: true }),
      ]);
      $("#drawer-title").textContent = `${trade.symbol} · ${trade.status}`;
      const row = (label, value) => `<div class="drawer-item"><span>${esc(label)}</span><strong>${value}</strong></div>`;
      const orderRows = (orders.items || []).map((order) => `
        <div class="tape-row"><span>${esc(order.kind)}</span><span class="muted">${esc(order.side)}</span>
        <span class="muted">${fmtNum(order.filled_quantity)} / ${fmtNum(order.quantity)}</span>
        <strong>${fmtPrice(order.price)}</strong><span>${pill(order.status, statusTone(order.status))}</span></div>`).join("");
      const auditRows = (audit.items || []).slice(0, 30).map((entry) => `
        <div class="tape-row"><span class="muted">${fmtDateTime(entry.created_at)}</span><span>${esc(entry.action)}</span>
        <strong class="muted">${esc(entry.actor || "system")}</strong></div>`).join("");
      $("#drawer-body").innerHTML = `
        <div class="drawer-grid">
          ${row("Planned entry", fmtPrice(trade.planned_entry))}
          ${row("Actual entry", fmtPrice(trade.actual_entry))}
          ${row("Entry source", esc(trade.entry_price_source || "Unavailable"))}
          ${row("Entry candle", trade.entry_candle_time ? fmtDateTime(trade.entry_candle_time) : "Unavailable")}
          ${row("Signal close", fmtPrice(trade.signal_close))}
          ${row("Price tick", esc(trade.price_tick || "Unavailable"))}
          ${row("Stop loss", `${fmtPrice(trade.stop_price)} ${trade.stop_stage ? `· ${esc(trade.stop_stage)}` : ""}`)}
          ${row("Target 1", `${fmtPrice(trade.target1_price)} ${trade.target1_quantity ? `· qty ${fmtNum(trade.target1_quantity)}` : ""}`)}
          ${row("Trailing", trade.trailing_enabled ? `${esc(trade.trailing_stop_percent || "—")}% · high ${fmtPrice(trade.trail_high_water_price)}` : "Disabled")}
          ${row("Quantity", `${fmtNum(trade.remaining_quantity)} of ${fmtNum(trade.quantity)}`)}
          ${row("Partial closed", trade.partial_closed ? `Yes · ${esc(trade.partial_close_percent)}%` : "No")}
          ${row("Net realized", trade.net_realized_pnl === null ? "Unavailable" : fmtMoney(trade.net_realized_pnl, "USDT", true))}
          ${row("Unrealized", trade.unrealized_pnl === null ? "Unavailable" : fmtMoney(trade.unrealized_pnl, "USDT", true))}
          ${row("Net profit", trade.net_profit_pnl !== null ? fmtMoney(trade.net_profit_pnl, "USDT", true) : fmtMoney((toNumber(trade.net_realized_pnl) || 0) + (toNumber(trade.unrealized_pnl) || 0), "USDT", true))}
          ${row("Protection", `${esc(trade.protection_state)} · ${esc(trade.protection_state_reason || "no reason recorded")}`)}
          ${row("Status reason", esc(trade.status_reason || "—"))}
        </div>
        <h3>ORDERS</h3>${orderRows || `<div class="muted">No orders recorded.</div>`}
        <h3>AUDIT TRAIL</h3>${auditRows || `<div class="muted">No audit entries.</div>`}
        <div class="drawer-actions">
          ${["OPEN", "PARTIALLY_CLOSED", "PROTECTION_ERROR", "UNKNOWN"].includes(trade.status) ? `<button class="button button-danger" data-action="close" data-id="${esc(trade.id)}" data-symbol="${esc(trade.symbol)}">Close position</button>` : ""}
          ${["QUEUED", "SUBMITTING", "ENTRY_PENDING", "ENTRY_PARTIAL"].includes(trade.status) ? `<button class="button button-secondary" data-action="cancel-entry" data-id="${esc(trade.id)}" data-symbol="${esc(trade.symbol)}">Cancel pending entry</button>` : ""}
        </div>`;
    } catch (error) {
      $("#drawer-body").innerHTML = `<div class="muted">${esc(describeError(error))}</div>`;
    }
  }

  /* ----------------------------------------------------------------------- modal --- */
  let modalResolver = null;

  function confirmModal({ title, copy, phrase, acknowledge = false, confirmLabel = "Confirm", eyebrow = "CONFIRM ACTION", symbol = "!" }) {
    return new Promise((resolve) => {
      modalResolver = resolve;
      $("#modal-title").textContent = title;
      $("#modal-copy").textContent = copy;
      $("#modal-eyebrow").textContent = eyebrow;
      $("#modal-symbol").textContent = symbol;
      $("#modal-confirm").textContent = confirmLabel;
      const phraseLabel = $("#modal-phrase-label");
      const ackRow = $("#modal-ack-row");
      if (phrase) {
        phraseLabel.classList.remove("hidden");
        $("#modal-phrase-display").textContent = phrase;
        $("#modal-phrase-input").value = "";
      } else {
        phraseLabel.classList.add("hidden");
      }
      ackRow.classList.toggle("hidden", !acknowledge);
      $("#modal-ack").checked = false;
      $("#modal-confirm").disabled = Boolean(phrase) || acknowledge;
      $("#modal-backdrop").classList.remove("hidden");
      setTimeout(() => (phrase ? $("#modal-phrase-input") : $("#modal-confirm")).focus(), 60);
    });
  }

  const closeModal = (result) => {
    $("#modal-backdrop").classList.add("hidden");
    if (modalResolver) { modalResolver(result); modalResolver = null; }
  };

  const modalReady = () => {
    const phrase = $("#modal-phrase-label").classList.contains("hidden") ? null : $("#modal-phrase-display").textContent;
    const phraseOk = !phrase || $("#modal-phrase-input").value.trim() === phrase;
    const ackOk = $("#modal-ack-row").classList.contains("hidden") || $("#modal-ack").checked;
    $("#modal-confirm").disabled = !(phraseOk && ackOk);
  };

  /* ------------------------------------------------------------------- palette ----- */
  const PALETTE_ITEMS = [
    { label: "Overview", hint: "view", run: () => setView("dashboard") },
    { label: "Positions", hint: "view", run: () => setView("positions") },
    { label: "Markets", hint: "view", run: () => setView("markets") },
    { label: "Signal log", hint: "view", run: () => setView("signals") },
    { label: "Trade history", hint: "view", run: () => setView("history") },
    { label: "Strategy playbook", hint: "view", run: () => setView("strategy") },
    { label: "Account & subscription", hint: "view", run: () => setView("account") },
    { label: "Settings", hint: "view", run: () => setView("settings") },
    { label: "Access control", hint: "view", run: () => setView("users") },
    { label: "Refresh all data", hint: "action", run: () => refreshAll() },
    { label: "Pause or resume trading", hint: "action", run: () => togglePause() },
    { label: "Cancel pending entries", hint: "action", run: () => cancelPending() },
    { label: "Switch to PAPER mode", hint: "action", run: () => switchPaper() },
    { label: "Sign out", hint: "action", run: () => signOut() },
  ];

  function openPalette() {
    $("#palette-backdrop").classList.remove("hidden");
    $("#palette-input").value = "";
    renderPalette("");
    setTimeout(() => $("#palette-input").focus(), 40);
  }

  const closePalette = () => $("#palette-backdrop").classList.add("hidden");

  function renderPalette(query) {
    const filter = query.trim().toLowerCase();
    const items = PALETTE_ITEMS.filter((item) => !filter || item.label.toLowerCase().includes(filter));
    state.paletteIndex = 0;
    $("#palette-results").innerHTML = items.map((item, index) => `
      <div class="palette-item ${index === 0 ? "selected" : ""}" data-index="${index}"><span>${esc(item.label)}</span><span>${esc(item.hint)}</span></div>`).join("")
      || `<div class="palette-item"><span>No matching command</span></div>`;
  }

  function runPaletteItem(index) {
    const filter = ($("#palette-input").value || "").trim().toLowerCase();
    const items = PALETTE_ITEMS.filter((item) => !filter || item.label.toLowerCase().includes(filter));
    const item = items[index];
    if (!item) return;
    closePalette();
    item.run();
  }

  /* ------------------------------------------------------------------ operations --- */
  async function togglePause() {
    const paused = Boolean(state.settings?.trading_paused);
    const phrase = paused ? "RESUME SHASHA ACT" : "PAUSE SHASHA ACT";
    const ok = await confirmModal({
      title: paused ? "Resume new signals?" : "Pause new signals?",
      copy: paused
        ? "Signal processing resumes immediately. Open positions keep their existing protection."
        : "New signals are rejected while paused. Existing positions keep their protection and can still be closed manually.",
      phrase,
      eyebrow: "EMERGENCY CONTROL",
      symbol: paused ? "▶" : "II",
      confirmLabel: paused ? "Resume trading" : "Pause trading",
    });
    if (!ok) return;
    try {
      await api("/api/control/pause", { method: "POST", body: { paused: !paused, confirmation_phrase: phrase } });
      toast(paused ? "Signal processing resumed." : "Signal processing paused.", "success");
      await refreshAll();
    } catch { /* toast already shown */ }
  }

  async function cancelPending() {
    const ok = await confirmModal({
      title: "Cancel every pending entry?",
      copy: "Only exchange-confirmed cancellations are counted. Pending limits that already filled will not be cancelled.",
      phrase: "CANCEL PENDING ENTRIES",
      eyebrow: "PENDING ENTRIES",
      symbol: "✕",
      confirmLabel: "Cancel entries",
    });
    if (!ok) return;
    try {
      const result = await api("/api/control/cancel-pending", { method: "POST", body: { confirmation_phrase: "CANCEL PENDING ENTRIES" } });
      toast(`${result.confirmed_cancellations} cancellation(s) confirmed.`, "success");
      await refreshAll();
    } catch { /* handled */ }
  }

  async function cancelEntry(tradeId, symbol) {
    const ok = await confirmModal({
      title: `Cancel pending entry for ${symbol}?`,
      copy: "The limit entry is cancelled only when the exchange confirms it. A partially filled entry keeps the filled part.",
      phrase: "CANCEL PENDING ENTRIES",
      eyebrow: "PENDING ENTRY",
      symbol: "✕",
      confirmLabel: "Cancel entry",
    });
    if (!ok) return;
    try {
      await api(`/api/trades/${encodeURIComponent(tradeId)}/cancel-entry`, { method: "POST", body: { confirmation_phrase: "CANCEL PENDING ENTRIES" } });
      toast("Cancellation request recorded.", "success");
      await refreshAll();
    } catch { /* handled */ }
  }

  async function closeTrade(tradeId, symbol) {
    const ok = await confirmModal({
      title: `Close ${symbol} at market?`,
      copy: "A reduce-only market close is requested. The position is finalized only after the exchange confirms the fill.",
      phrase: "CLOSE POSITION",
      eyebrow: "REDUCE-ONLY CLOSE",
      symbol: "⏻",
      confirmLabel: "Close position",
    });
    if (!ok) return;
    try {
      const result = await api(`/api/trades/${encodeURIComponent(tradeId)}/close`, { method: "POST", body: { confirmation_phrase: "CLOSE POSITION" } });
      toast(result.message || "Close request recorded.", "success");
      $("#drawer-backdrop").classList.add("hidden");
      await refreshAll();
    } catch { /* handled */ }
  }

  async function switchPaper() {
    const ok = await confirmModal({
      title: "Switch to PAPER mode?",
      copy: "PAPER mode never calls private order endpoints. It can only be selected when no LIVE trade is open or unresolved.",
      phrase: "SWITCH TO PAPER",
      eyebrow: "TRADING MODE",
      symbol: "◐",
      confirmLabel: "Switch to PAPER",
    });
    if (!ok) return;
    try {
      await api("/api/control/mode/paper", { method: "POST", body: { confirmation_phrase: "SWITCH TO PAPER" } });
      toast("PAPER mode active.", "success");
      await refreshAll();
    } catch { /* handled */ }
  }

  async function enableLive() {
    const ok = await confirmModal({
      title: "Enable LIVE trading?",
      copy: "LIVE submits real orders to Shark with real margin at risk. This requires verified contract filters, verified split protection semantics and an open server gate.",
      phrase: "ENABLE LIVE TRADING",
      acknowledge: true,
      eyebrow: "IRREVERSIBLE RISK",
      symbol: "⚠",
      confirmLabel: "Enable LIVE",
    });
    if (!ok) return;
    try {
      await api("/api/control/mode/live", {
        method: "POST",
        body: { confirmation_phrase: "ENABLE LIVE TRADING", acknowledge_exchange_risk: true },
      });
      toast("LIVE mode confirmed.", "success");
      await refreshAll();
    } catch { /* handled */ }
  }

  async function acceptDisclaimer() {
    try {
      await api("/api/auth/disclaimer", { method: "POST" });
      toast("Risk disclaimer acknowledged.", "success");
      await loadIdentity();
      renderAccount();
    } catch { /* handled */ }
  }

  async function editSubscription() {
    const service = state.subscription || {};
    const plan = window.prompt("Subscription plan name", service.plan_name || "SHASHA ACT PRO");
    if (plan === null) return;
    const fee = window.prompt("Subscription fee (numbers only)", service.fee ?? "999");
    if (fee === null) return;
    const currency = window.prompt("Currency", service.currency || "INR");
    if (currency === null) return;
    const cycle = window.prompt("Billing cycle (MONTHLY, QUARTERLY, YEARLY, LIFETIME)", service.billing_cycle || "MONTHLY");
    if (cycle === null) return;
    try {
      await api("/api/subscription", { method: "PATCH", body: { plan_name: plan, fee, currency, billing_cycle: cycle.toUpperCase() } });
      toast("Subscription updated.", "success");
      await refreshAll();
    } catch { /* handled */ }
  }

  async function renewSubscription() {
    const current = state.userSubscription?.expires_at || state.subscription?.expires_at;
    const base = current ? new Date(current) : new Date();
    const next = new Date(Math.max(base.getTime(), Date.now()) + 30 * 86400000);
    const value = window.prompt("New expiry date (YYYY-MM-DD)", next.toISOString().slice(0, 10));
    if (!value) return;
    try {
      await api("/api/subscription", { method: "PATCH", body: { expires_at: `${value}T23:59:59+00:00` } });
      toast("Renewal window extended.", "success");
      await refreshAll();
    } catch { /* handled */ }
  }

  async function createUser(form) {
    const status = $("#user-save-status");
    status.textContent = "Creating…";
    status.className = "form-status";
    const expires = form.elements.subscription_expires_at.value;
    try {
      await api("/api/users", {
        method: "POST",
        body: {
          username: form.elements.username.value.trim(),
          password: form.elements.password.value,
          role: form.elements.role.value,
          subscription_plan: form.elements.subscription_plan.value.trim() || null,
          subscription_fee: form.elements.subscription_fee.value || null,
          subscription_currency: form.elements.subscription_currency.value.trim().toUpperCase() || null,
          subscription_expires_at: expires ? `${expires}T23:59:59+00:00` : null,
        },
      });
      status.textContent = "Operator created.";
      status.className = "form-status ok";
      form.reset();
      await renderUsers();
    } catch (error) {
      status.textContent = describeError(error);
      status.className = "form-status error";
    }
  }

  async function editUser(userId, username, role, active) {
    const nextRole = window.prompt(`Role for ${username} (VIEWER, OPERATOR, ADMIN, OWNER)`, role);
    if (nextRole === null) return;
    const activeAnswer = window.prompt(`Account active? Type yes or no`, active ? "yes" : "no");
    if (activeAnswer === null) return;
    const expiry = window.prompt("Subscription expiry (YYYY-MM-DD, blank to keep)", "");
    const body = { role: nextRole.trim().toUpperCase(), is_active: activeAnswer.trim().toLowerCase().startsWith("y") };
    if (expiry) body.subscription_expires_at = `${expiry}T23:59:59+00:00`;
    try {
      await api(`/api/users/${encodeURIComponent(userId)}`, { method: "PATCH", body });
      toast("Operator updated.", "success");
      await renderUsers();
    } catch { /* handled */ }
  }

  async function toggleDashboardAuth() {
    const enabled = Boolean(state.settings?.dashboard_auth_enabled);
    const ok = await confirmModal({
      title: enabled ? "Disable dashboard authentication?" : "Enable dashboard authentication?",
      copy: enabled
        ? "Every same-origin caller will be able to reach the console and API. Only do this on a trusted, non-exposed host."
        : "Every dashboard request will then require a session token or the server access token.",
      acknowledge: enabled,
      eyebrow: "ACCESS CONTROL",
      symbol: "⛭",
      confirmLabel: enabled ? "Disable auth" : "Enable auth",
    });
    if (!ok) return;
    try {
      await api("/api/control/dashboard-auth", { method: "POST", body: { enabled: !enabled } });
      toast(`Dashboard authentication ${enabled ? "disabled" : "enabled"}.`, "success");
      await refreshAll();
    } catch { /* handled */ }
  }

  /* -------------------------------------------------------------------- refresh ---- */
  async function refreshAll() {
    const tasks = [
      api("/api/settings", { silent: true }).then((data) => { state.settings = data; }),
      api("/api/overview", { silent: true }).then((data) => { state.overview = data; }),
      api("/api/positions", { silent: true }).then((data) => { state.positions = data.items || []; }),
      api("/api/pending-entries", { silent: true }).then((data) => { state.pendingEntries = data.items || []; }),
      api("/api/health", { silent: true }).then((data) => { state.health = data; }),
      api(`/api/performance?days=${state.performanceDays}`, { silent: true }).then((data) => { state.performance = data; }),
      api("/api/subscription", { silent: true }).then((data) => { state.userSubscription = data.user; if (data.service) state.subscription = data.service; }),
    ];
    if (state.view === "signals" || state.firstPaint) tasks.push(refreshSignals());
    if (state.view === "history" || state.firstPaint) tasks.push(refreshHistory());
    if (state.view === "markets" || state.firstPaint) tasks.push(refreshMarkets());
    const results = await Promise.allSettled(tasks);
    const failed = results.filter((result) => result.status === "rejected");
    state.online = failed.length < results.length;
    state.lastUpdate = new Date().toISOString();
    renderDashboard();
    renderPositions();
    renderStrategy();
    renderAccount();
    renderSettings();
    renderPerformance();
    renderConnectivity();
    if (state.view === "users") await renderUsers();
    if (state.firstPaint) { state.firstPaint = false; }
    if (failed.length === results.length && results.length) toast("Every data request failed. Check the server connection.", "error");
  }

  /* ----------------------------------------------------------------- performance -- */
  function renderPerformance() {
    const series = state.performance?.series || [];
    const chart = $("#performance-chart");
    if (!series.length) {
      chart.innerHTML = "";
      $("#performance-total").textContent = "Unavailable";
      $("#performance-period").textContent = "No persisted fills in this window";
      return;
    }
    const values = series.map((point) => toNumber(point.net_realized_pnl) ?? 0);
    const total = values.reduce((sum, value) => sum + value, 0);
    const max = Math.max(...values, 0);
    const min = Math.min(...values, 0);
    const span = Math.max(max - min, 0.0001);
    chart.innerHTML = series.map((point, index) => {
      const value = values[index];
      const height = Math.max((Math.abs(value) / span) * 100, value === 0 ? 1 : 2);
      const className = value > 0 ? "bar" : value < 0 ? "bar loss" : "bar zero";
      return `<span class="${className}" style="height:${height}%;animation-delay:${index * 8}ms" title="${esc(point.date)} · ${fmtMoney(value, "USDT", true)}"></span>`;
    }).join("");
    $("#performance-total").textContent = fmtMoney(total, "USDT", true);
    $("#performance-total").className = pnlClass(total);
    $("#performance-period").textContent = `Last ${series.length} days · fees included${state.performance.funding_included ? "" : " · funding not included"}`;
    $("#chart-max").textContent = fmtNum(max, 2);
    $("#chart-mid").textContent = fmtNum((max + min) / 2, 2);
    $("#chart-min").textContent = fmtNum(min, 2);
    $("#chart-start").textContent = series[0].date;
    $("#chart-end").textContent = series[series.length - 1].date;
    $("#performance-mode").textContent = state.performance.mode || "PAPER";
    const zero = $("#chart-zero");
    zero.style.top = `${(max / span) * 100}%`;
  }

  /* ---------------------------------------------------------------- neural canvas -- */
  function startNeuralField() {
    const canvas = $("#neural-field");
    if (!canvas || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    let width = 0; let height = 0; let nodes = []; let pulses = [];
    const pointer = { x: 0.5, y: 0.4, active: false };

    const resize = () => {
      width = canvas.width = window.innerWidth * window.devicePixelRatio;
      height = canvas.height = window.innerHeight * window.devicePixelRatio;
      canvas.style.width = `${window.innerWidth}px`;
      canvas.style.height = `${window.innerHeight}px`;
      const count = Math.min(Math.round((window.innerWidth * window.innerHeight) / 26000), 96);
      nodes = Array.from({ length: count }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.22 * window.devicePixelRatio,
        vy: (Math.random() - 0.5) * 0.22 * window.devicePixelRatio,
        r: (Math.random() * 1.6 + 0.7) * window.devicePixelRatio,
      }));
    };

    window.addEventListener("resize", resize, { passive: true });
    window.addEventListener("pointermove", (event) => {
      pointer.x = event.clientX / window.innerWidth;
      pointer.y = event.clientY / window.innerHeight;
      pointer.active = true;
    }, { passive: true });
    resize();

    const draw = () => {
      context.clearRect(0, 0, width, height);
      for (const node of nodes) {
        node.x += node.vx; node.y += node.vy;
        if (node.x < 0 || node.x > width) node.vx *= -1;
        if (node.y < 0 || node.y > height) node.vy *= -1;
        if (pointer.active) {
          node.x += (pointer.x * width - node.x) * 0.00035;
          node.y += (pointer.y * height - node.y) * 0.00035;
        }
      }
      const limit = 168 * window.devicePixelRatio;
      for (let index = 0; index < nodes.length; index += 1) {
        for (let other = index + 1; other < nodes.length; other += 1) {
          const dx = nodes[index].x - nodes[other].x;
          const dy = nodes[index].y - nodes[other].y;
          const distance = Math.hypot(dx, dy);
          if (distance > limit) continue;
          const alpha = (1 - distance / limit) * 0.4;
          const grad = context.createLinearGradient(nodes[index].x, nodes[index].y, nodes[other].x, nodes[other].y);
          grad.addColorStop(0, `rgba(34, 211, 238, ${alpha})`);
          grad.addColorStop(1, `rgba(139, 92, 246, ${alpha * 0.8})`);
          context.strokeStyle = grad;
          context.lineWidth = 0.6 * window.devicePixelRatio;
          context.beginPath();
          context.moveTo(nodes[index].x, nodes[index].y);
          context.lineTo(nodes[other].x, nodes[other].y);
          context.stroke();
        }
      }
      for (const node of nodes) {
        context.beginPath();
        context.fillStyle = "rgba(160, 220, 255, 0.75)";
        context.arc(node.x, node.y, node.r, 0, Math.PI * 2);
        context.fill();
      }
      if (!pulses.length || Math.random() < 0.02) {
        const start = nodes[Math.floor(Math.random() * nodes.length)];
        pulses.push({ node: start, life: 1 });
      }
      pulses = pulses.filter((pulse) => pulse.life > 0 && nodes.includes(pulse.node));
      for (const pulse of pulses) {
        pulse.life -= 0.012;
        context.beginPath();
        context.fillStyle = `rgba(34, 211, 238, ${pulse.life * 0.8})`;
        context.arc(pulse.node.x, pulse.node.y, 2.4 * window.devicePixelRatio, 0, Math.PI * 2);
        context.fill();
      }
      requestAnimationFrame(draw);
    };
    requestAnimationFrame(draw);
  }

  /* ----------------------------------------------------------------------- clock --- */
  function startClock() {
    const tick = () => {
      const now = new Date();
      const ist = new Date(now.getTime() + (330 + now.getTimezoneOffset()) * 60000);
      const clock = ist.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) + " IST";
      $("#market-clock").textContent = clock;
      const label = $("#market-clock-label");
      if (label) label.textContent = `MARKET DAY ${ist.toISOString().slice(0, 10)}`;
      [["#last-updated"], ["#sidebar-market"]].forEach(() => {});
    };
    tick();
    setInterval(tick, 1000);
  }

  /* -------------------------------------------------------------------- polling ---- */
  function startPolling() {
    stopPolling();
    state.polling = setInterval(async () => {
      if (document.hidden) return;
      try {
        await refreshAll();
        if (state.liveFeed) await refreshLiveFeed();
        await recordSentiment();
      } catch { /* individual requests already reported */ }
    }, POLL_MS);
  }

  function stopPolling() {
    if (state.polling) { clearInterval(state.polling); state.polling = null; }
  }

  /* --------------------------------------------------------------------- events ---- */
  function bindEvents() {
    document.body.addEventListener("click", (event) => {
      const navTarget = event.target.closest("[data-view]");
      if (navTarget && !event.target.closest("[data-action]")) { setView(navTarget.dataset.view); return; }
      const actionTarget = event.target.closest("[data-action]");
      if (actionTarget) {
        const { action, id, symbol, role, active, username } = actionTarget.dataset;
        if (action === "details") openDrawer(id);
        if (action === "close") closeTrade(id, symbol);
        if (action === "cancel-entry") cancelEntry(id, symbol);
        if (action === "retry-notice") api(`/api/events/${encodeURIComponent(id)}/retry-notification`, { method: "POST" })
          .then(() => { toast("Notification retry queued.", "success"); refreshSignals(); })
          .catch(() => {});
        if (action === "edit-user") editUser(id, username, role, active === "true");
        return;
      }
      if (event.target.closest("th.sortable")) {
        const th = event.target.closest("th.sortable");
        const sortKey = th.dataset.sort;
        if (state.positionSort === sortKey) {
          state.positionOrder = state.positionOrder === "asc" ? "desc" : "asc";
        } else {
          state.positionSort = sortKey;
          state.positionOrder = ["symbol", "status", "protection"].includes(sortKey) ? "asc" : "desc";
        }
        renderPositions();
        return;
      }
      if (event.target.closest(".filter-chip")) {
        const chip = event.target.closest(".filter-chip");
        const row = chip.parentElement.id;
        Array.from(chip.parentElement.children).forEach((node) => node.classList.toggle("active", node === chip));
        if (row === "position-filters") { state.positionFilter = chip.dataset.filter; renderPositions(); }
        if (row === "signal-filters") { state.signalStatus = chip.dataset.status || ""; state.signalsOffset = 0; refreshSignals(); }
        return;
      }
      const authTab = event.target.closest(".auth-tab");
      if (authTab) {
        $$(".auth-tab").forEach((tab) => tab.classList.toggle("active", tab === authTab));
        const mode = authTab.dataset.authMode;
        $("#auth-password-fields").classList.toggle("hidden", mode !== "password");
        $("#auth-token-fields").classList.toggle("hidden", mode !== "token");
        return;
      }
      const paletteItem = event.target.closest(".palette-item");
      if (paletteItem && paletteItem.dataset.index) { runPaletteItem(Number(paletteItem.dataset.index)); return; }
      if (event.target.id === "palette-backdrop") closePalette();
      if (event.target.id === "drawer-backdrop") $("#drawer-backdrop").classList.add("hidden");
      if (event.target.id === "modal-backdrop") closeModal(false);
    });

    $("#auth-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const mode = $(".auth-tab.active")?.dataset.authMode || "password";
      const accept = $("#auth-disclaimer-accept");
      if (!accept.checked) { $("#auth-error").textContent = "Accept the risk disclaimer to continue."; return; }
      if (mode === "password") {
        signIn("password", { username: $("#auth-username").value.trim(), password: $("#auth-password").value });
      } else {
        signIn("token", $("#auth-token").value);
      }
    });

    $("#signout-button").addEventListener("click", signOut);
    $("#account-signout").addEventListener("click", signOut);
    $("#refresh-button").addEventListener("click", () => refreshAll().then(() => toast("Data refreshed.", "success")));
    $("#pause-button").addEventListener("click", togglePause);
    $("#settings-pause-button").addEventListener("click", togglePause);
    $("#cancel-pending-button").addEventListener("click", cancelPending);
    $("#positions-refresh").addEventListener("click", () => refreshAll().then(() => toast("Positions refreshed.", "success")));
    $("#switch-paper-button").addEventListener("click", switchPaper);
    $("#enable-live-button").addEventListener("click", enableLive);
    $("#toggle-auth-btn").addEventListener("click", toggleDashboardAuth);
    $("#clear-saved-token-btn").addEventListener("click", () => {
      localStorage.removeItem(SESSION_STORAGE_KEY);
      localStorage.removeItem(TOKEN_STORAGE_KEY);
      toast("Saved browser credentials cleared.", "success");
    });
    $("#accept-disclaimer").addEventListener("click", acceptDisclaimer);
    $("#renew-button").addEventListener("click", renewSubscription);
    $("#edit-subscription-button").addEventListener("click", editSubscription);
    $("#command-button").addEventListener("click", openPalette);
    $("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
    $("#drawer-close").addEventListener("click", () => $("#drawer-backdrop").classList.add("hidden"));
    $("#modal-close").addEventListener("click", () => closeModal(false));
    $("#modal-cancel").addEventListener("click", () => closeModal(false));
    $("#modal-confirm").addEventListener("click", () => closeModal(true));
    $("#modal-phrase-input").addEventListener("input", modalReady);
    $("#modal-ack").addEventListener("change", modalReady);
    $("#dismiss-ticker").addEventListener("click", () => $("#disclaimer-ticker").classList.add("hidden"));
    $("#dismiss-alert").addEventListener("click", () => $("#system-alert").classList.add("hidden"));
    $("#palette-input").addEventListener("input", (event) => renderPalette(event.target.value));

    $("#strategy-form").addEventListener("submit", (event) => { event.preventDefault(); saveStrategy(event.target); });
    $("#risk-form").addEventListener("submit", (event) => { event.preventDefault(); saveRisk(event.target); });
    $("#user-form").addEventListener("submit", (event) => { event.preventDefault(); createUser(event.target); });
    $("#symbol-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = $("#symbol-save-status");
      try {
        await api("/api/settings", {
          method: "PATCH",
          body: {
            allow_symbols: event.target.elements.allow_symbols.value.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean),
            deny_symbols: event.target.elements.deny_symbols.value.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean),
          },
        });
        status.textContent = "Symbol filters saved.";
        status.className = "form-status ok";
        await refreshAll();
      } catch (error) { status.textContent = describeError(error); status.className = "form-status error"; }
    });

    $("#performance-range").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-days]");
      if (!button) return;
      $$("#performance-range button").forEach((node) => node.classList.toggle("selected", node === button));
      state.performanceDays = Number(button.dataset.days);
      prefs.save({ performanceDays: state.performanceDays });
      refreshAll();
    });

    $("#history-mode").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-mode]");
      if (!button) return;
      $$("#history-mode button").forEach((node) => node.classList.toggle("selected", node === button));
      state.historyMode = button.dataset.mode;
      state.historyOffset = 0;
      prefs.save({ historyMode: state.historyMode });
      refreshHistory();
    });

    $("#history-prev").addEventListener("click", () => { state.historyOffset = Math.max(0, state.historyOffset - state.historyLimit); refreshHistory(); });
    $("#history-next").addEventListener("click", () => { state.historyOffset += state.historyLimit; refreshHistory(); });
    $("#signals-prev").addEventListener("click", () => { state.signalsOffset = Math.max(0, state.signalsOffset - state.signalsLimit); refreshSignals(); });
    $("#signals-next").addEventListener("click", () => { state.signalsOffset += state.signalsLimit; refreshSignals(); });

    $("#market-search").addEventListener("input", debounce((event) => { state.marketQuery = event.target.value.trim(); refreshMarkets(); }, 300));
    $("#market-sort").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-sort]");
      if (!button) return;
      $$("#market-sort button").forEach((node) => node.classList.toggle("selected", node === button));
      state.marketSort = button.dataset.sort;
      refreshMarkets();
    });
    $("#market-direction-toggle").addEventListener("click", () => {
      state.marketOrder = state.marketOrder === "desc" ? "asc" : "desc";
      $("#market-direction-label").textContent = state.marketOrder.toUpperCase();
      refreshMarkets();
    });
    $("#live-feed-toggle").addEventListener("click", (event) => {
      state.liveFeed = !state.liveFeed;
      event.currentTarget.classList.toggle("active", state.liveFeed);
      if (state.liveFeed) refreshLiveFeed();
      else $("#market-tape").innerHTML = `<div class="muted">Live tape off</div>`;
      refreshSentimentHistory();
    });

    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openPalette(); return; }
      if (event.key === "Escape") { closePalette(); closeModal(false); $("#drawer-backdrop").classList.add("hidden"); return; }
      if (!$("#palette-backdrop").classList.contains("hidden")) {
        const items = $$(".palette-item");
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const selected = $(".palette-item.selected");
          const index = Math.max(0, Math.min(items.length - 1, (Number(selected?.dataset.index) || 0) + (event.key === "ArrowDown" ? 1 : -1)));
          items.forEach((node) => node.classList.toggle("selected", node === items[index]));
          return;
        }
        if (event.key === "Enter") { event.preventDefault(); runPaletteItem(Number($(".palette-item.selected")?.dataset.index) || 0); }
      }
    });

    document.addEventListener("visibilitychange", () => { if (!document.hidden && state.user !== null) refreshAll(); });
    window.addEventListener("online", () => { state.online = true; refreshAll(); });
    window.addEventListener("offline", () => { state.online = false; renderConnectivity(); });
  }

  const debounce = (fn, wait) => {
    let handle = null;
    return (...args) => { clearTimeout(handle); handle = setTimeout(() => fn(...args), wait); };
  };

  /* ------------------------------------------------------------------------ init --- */
  async function bootstrapDashboard() {
    await refreshAll();
    renderFundamentals();
  }

  function renderFundamentals() {
    const user = state.user;
    if ($("#nav-users")) $("#nav-users").classList.toggle("hidden", !state.permissions.can_manage_users);
    if (user) {
      $("#user-name").textContent = user.display_name || user.username;
      $("#user-role").textContent = user.role;
      $("#user-avatar").textContent = user.username.slice(0, 2).toUpperCase();
    }
    ["#edit-subscription-button"].forEach((selector) => { const node = $(selector); if (node) node.disabled = !state.permissions.can_change_settings; });
  }

  async function init() {
    startNeuralField();
    startClock();
    bindEvents();
    setView(state.view);
    $("#market-direction-label").textContent = state.marketOrder.toUpperCase();
    await loadPublicConfig();
    if (!state.sessionToken && !state.serverToken) { showAuth(); return; }
    try {
      await loadIdentity();
      hideAuth();
      await bootstrapDashboard();
      startPolling();
    } catch (error) {
      clearCredentials();
      showAuth(describeError(error));
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
