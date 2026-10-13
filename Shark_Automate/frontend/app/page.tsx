"use client";

import { fmtNum, fmtPnl, fmtPrice, fmtTime, apiSend, usePolling } from "@/lib/api";
import type { HealthResponse, PositionRow, SignalRow, StatusResponse, TradeRow } from "@/lib/types";
import { Panel, Stat, SymTriple, StatusBadge, pnlTone, EmptyState, ErrorState } from "@/components/ui";

export default function Dashboard() {
  const status = usePolling<StatusResponse>("/api/status", 3000);
  const health = usePolling<HealthResponse>("/api/health", 5000);
  const positions = usePolling<PositionRow[]>("/api/positions", 3000);
  const signals = usePolling<SignalRow[]>("/api/signals?limit=8", 3000);
  const trades = usePolling<TradeRow[]>("/api/trades?limit=8", 4000);

  const s = status.data;
  const recentTrades = trades.data ?? [];
  const todayClosed = recentTrades.filter((t) => {
    if (!t.closed_at) return false;
    return Date.now() - new Date(t.closed_at).getTime() < 86400000;
  });

  return (
    <div className="space-y-4">
      {/* MODE BANNER — visually unmistakable */}
      <div
        className={`rounded border px-4 py-3 flex items-center justify-between ${
          s?.mode === "LIVE"
            ? "border-live bg-loss/10"
            : "border-info/50 bg-info/5"
        }`}
      >
        <div className="flex items-center gap-3">
          <span
            className={`text-lg font-bold tracking-widest ${s?.mode === "LIVE" ? "text-loss animate-pulse" : "text-info"}`}
          >
            {s?.mode === "LIVE" ? "● LIVE MODE" : "● PAPER MODE"}
          </span>
          <span className="text-[11px] text-muted">
            {s?.mode === "LIVE"
              ? "REAL MONEY — orders go to Shark Exchange"
              : "simulation — real signals & market prices, zero real orders"}
            {s?.dry_run && s?.mode === "LIVE" ? " · DRY RUN (payloads logged, NOT submitted)" : ""}
          </span>
        </div>
        <div className="flex gap-2">
          {s?.halted && <span className="badge badge-amber">TRADING HALTED</span>}
          {s?.kill_switch && <span className="badge badge-red">KILL SWITCH ON</span>}
        </div>
      </div>

      {/* CONNECTION HEALTH */}
      <Panel title="System Health">
        <div className="flex flex-wrap gap-2">
          {Object.entries(health.data?.components ?? {}).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 panel2 bg-panel2 border border-border rounded px-2.5 py-1.5">
              <span className="th normal-case">{k.replace(/_/g, " ")}</span>
              <StatusBadge status={String(v)} />
            </div>
          ))}
          {health.error && <ErrorState message={health.error} />}
        </div>
      </Panel>

      {/* RISK + PNL */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Panel title="Paper Wallet & Risk Status">
          <div className="grid grid-cols-2 gap-2">
            <Stat
              label="Wallet Balance"
              value={`${fmtNum(s?.balance?.wallet_balance ?? 10000)} ${s?.risk.account_unit ?? "USDT"}`}
              tone="text-info"
            />
            <Stat
              label="Available Margin"
              value={`${fmtNum(s?.balance?.available_margin ?? 10000)} ${s?.risk.account_unit ?? "USDT"}`}
              tone="text-profit"
            />
            <Stat
              label="Open Positions"
              value={`${s?.risk.open_positions ?? 0}`}
            />
            <Stat label="Available Slots" value={s?.risk.available_slots ?? 200} tone="text-profit" />
            <Stat label="Margin / Trade" value={`${fmtNum(s?.risk.margin_per_trade)} ${s?.risk.account_unit ?? "USDT"}`} />
            <Stat label="Leverage" value={`${s?.risk.leverage ?? 10}x`} />
          </div>
          <div className="mt-2 text-[11px] text-muted">
            All VIP channel signals accepted · Margin per trade: {fmtNum(s?.risk.margin_per_trade)} USDT · SHORT:{" "}
            {s?.risk.short_enabled ? "enabled" : "disabled"}
          </div>
        </Panel>


        <Panel title="P&L Overview" className="xl:col-span-2">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Stat label="Today P&L" value={fmtPnl(s?.pnl.today_pnl)} tone={pnlTone(s?.pnl.today_pnl ?? 0)} />
            <Stat label="Realized P&L" value={fmtPnl(s?.pnl.realized_pnl)} tone={pnlTone(s?.pnl.realized_pnl ?? 0)} />
            <Stat label="Unrealized P&L" value={fmtPnl(s?.pnl.unrealized_pnl)} tone={pnlTone(s?.pnl.unrealized_pnl ?? 0)} />
            <Stat label="Total Trades" value={s?.pnl.total_trades ?? 0} />
            <Stat label="Winning Trades" value={s?.pnl.winning_trades ?? 0} tone="text-profit" />
            <Stat label="Losing Trades" value={s?.pnl.losing_trades ?? 0} tone="text-loss" />
            <Stat label="Win Rate" value={`${fmtNum(s?.pnl.win_rate)}%`} />
            <div className="grid grid-rows-2 gap-2">
              <Stat label="Avg Profit" value={fmtPnl(s?.pnl.average_profit)} tone="text-profit" />
              <Stat label="Avg Loss" value={fmtPnl(s?.pnl.average_loss)} tone="text-loss" />
            </div>
          </div>
        </Panel>
      </div>

      {/* ACTIVE POSITIONS */}
      <Panel title="Active Positions" right={<span className="text-[11px] text-muted">ROI on margin · Price Δ shown separately</span>}>
        {(positions.data ?? []).length === 0 ? (
          <EmptyState message="No active positions. Waiting for TradingView signals…" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-border">
                  {["Symbols (TV / Canonical / Shark)", "Side", "Entry", "Current", "Qty", "Margin (USDT)", "Lev", "P&L (USDT)", "ROI", "Price Δ", "TP Target", "Distance", "Duration", "Status"].map(
                    (h) => (
                      <th key={h} className="th px-3 py-2">{h}</th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {(positions.data ?? []).map((p) => {
                  const roi = parseFloat(p.roi_percent || "0");
                  const entry = parseFloat(p.entry_price || "0");
                  const mark = parseFloat(p.mark_price || "0");
                  const priceChange = entry ? ((mark - entry) / entry) * 100 : 0;
                  const target = parseFloat(p.target_price || "0");
                  const distance = (target && mark)
                    ? (p.direction === "SHORT" ? ((mark - target) / mark) * 100 : ((target - mark) / mark) * 100)
                    : (roi - parseFloat(p.target_roi_percent || "10"));
                  const dur = p.open_time ? Math.floor((Date.now() - new Date(p.open_time).getTime()) / 1000) : 0;
                  return (
                    <tr key={p.position_id} className="border-b border-border/50 row-hover">
                      <td className="td"><SymTriple raw={p.tradingview_raw_symbol} canonical={p.canonical_symbol} shark={p.shark_symbol} /></td>
                      <td className="td font-semibold">{p.direction}</td>
                      <td className="td font-mono">{fmtPrice(p.entry_price)}</td>
                      <td className="td font-mono">{fmtPrice(p.mark_price)}</td>
                      <td className="td font-mono">{fmtNum(p.quantity, 4)}</td>
                      <td className="td font-mono">{fmtNum(p.margin)}</td>
                      <td className="td">{p.leverage}x</td>
                      <td className={`td font-mono font-semibold ${pnlTone(p.unrealized_pnl)}`}>{fmtPnl(p.unrealized_pnl)}</td>
                      <td className={`td font-mono font-semibold ${pnlTone(roi)}`}>{fmtNum(roi)}%</td>
                      <td className={`td font-mono ${pnlTone(priceChange)}`}>{fmtNum(priceChange)}%</td>
                      <td className="td font-mono text-profit">{fmtPrice(p.target_price)}</td>
                      <td className={`td font-mono ${distance >= 0 ? "text-profit" : "text-warn"}`}>{fmtNum(distance)}%</td>
                      <td className="td font-mono text-muted">{Math.floor(dur / 60)}m {dur % 60}s</td>
                      <td className="td"><StatusBadge status={p.state} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <Panel title="Recent Signals">
          {(signals.data ?? []).length === 0 ? (
            <EmptyState message="No signals received yet." />
          ) : (
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-border">
                  {["Time", "Strategy", "Symbols", "Action", "Status", "Reason"].map((h) => (
                    <th key={h} className="th px-2 py-1.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(signals.data ?? []).map((sig) => (
                  <tr key={sig.id} className="border-b border-border/50 row-hover">
                    <td className="td font-mono text-muted">{fmtTime(sig.received_at)}</td>
                    <td className="td">{sig.strategy_name || sig.strategy_id}</td>
                    <td className="td"><SymTriple raw={sig.tradingview_raw_symbol} canonical={sig.canonical_symbol} shark={sig.shark_symbol} /></td>
                    <td className="td font-semibold">{sig.action}</td>
                    <td className="td"><StatusBadge status={sig.status_display} /></td>
                    <td className="td text-muted">{sig.rejection_reason || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Today&apos;s Closed Trades">
          {todayClosed.length === 0 ? (
            <EmptyState message="No closed trades today." />
          ) : (
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-border">
                  {["Closed", "Symbol", "Entry → Exit", "Net P&L", "ROI", "Reason"].map((h) => (
                    <th key={h} className="th px-2 py-1.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {todayClosed.map((t) => (
                  <tr key={t.trade_id} className="border-b border-border/50 row-hover">
                    <td className="td font-mono text-muted">{fmtTime(t.closed_at)}</td>
                    <td className="td font-mono">{t.canonical_symbol}</td>
                    <td className="td font-mono">{fmtPrice(t.entry_price)} → {fmtPrice(t.exit_price)}</td>
                    <td className={`td font-mono font-semibold ${pnlTone(t.net_pnl)}`}>{fmtPnl(t.net_pnl)}</td>
                    <td className={`td font-mono ${pnlTone(t.roi_percent)}`}>{fmtNum(t.roi_percent)}%</td>
                    <td className="td text-muted">{t.close_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  );
}
