"use client";

import { useState } from "react";
import { fmtNum, fmtPnl, fmtPrice, fmtTime, usePolling } from "@/lib/api";
import type { OrderRow, TradeRow } from "@/lib/types";
import { Panel, SymTriple, StatusBadge, EmptyState, ErrorState, pnlTone } from "@/components/ui";

export default function TradesPage() {
  const trades = usePolling<TradeRow[]>("/api/trades?limit=300", 5000);
  const orders = usePolling<OrderRow[]>("/api/orders?limit=100", 4000);
  const [tab, setTab] = useState<"trades" | "orders">("trades");
  const [mode, setMode] = useState("");
  const [result, setResult] = useState("");
  const [symbol, setSymbol] = useState("");

  const rows = (trades.data ?? []).filter((t) => {
    if (mode && t.mode !== mode) return false;
    if (result && t.result !== result) return false;
    if (!symbol) return true;
    const q = symbol.toUpperCase();
    return t.tradingview_raw_symbol.includes(q) || t.canonical_symbol.includes(q) || t.shark_symbol.includes(q);
  });

  return (
    <div className="space-y-4">
      <Panel
        title="Execution History"
        right={
          <div className="flex gap-2">
            <button className={`btn ${tab === "trades" ? "btn-ok" : ""}`} onClick={() => setTab("trades")}>Trades</button>
            <button className={`btn ${tab === "orders" ? "btn-ok" : ""}`} onClick={() => setTab("orders")}>Orders</button>
            <a className="btn" href="/api/trades/export/csv" download>Export CSV</a>
          </div>
        }
      >
        {tab === "trades" && (
          <>
            <div className="flex gap-2 mb-3">
              <input className="input w-44" placeholder="Symbol filter…" value={symbol} onChange={(e) => setSymbol(e.target.value)} />
              <select className="input" value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="">All modes</option>
                <option value="PAPER">PAPER</option>
                <option value="LIVE">LIVE</option>
              </select>
              <select className="input" value={result} onChange={(e) => setResult(e.target.value)}>
                <option value="">Win/Loss</option>
                <option value="WIN">WIN</option>
                <option value="LOSS">LOSS</option>
                <option value="FLAT">FLAT</option>
              </select>
            </div>
            {trades.error && <ErrorState message={trades.error} />}
            {rows.length === 0 ? (
              <EmptyState message="No closed trades." />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left">
                  <thead>
                    <tr className="border-b border-border">
                      {["Timestamp", "Strategy", "TV Symbol", "Canonical", "Shark", "Side", "Entry", "Exit", "Qty", "Margin", "Lev", "Entry Fee", "Exit Fee", "Funding", "Gross P&L", "Net P&L", "ROI", "Price Δ", "Duration", "Mode", "Status", "Event ID", "Exit Order"].map((h) => (
                        <th key={h} className="th px-2 py-2">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((t) => (
                      <tr key={t.trade_id} className="border-b border-border/50 row-hover">
                        <td className="td font-mono text-muted">{fmtTime(t.closed_at)}</td>
                        <td className="td">{t.strategy_id || "—"}</td>
                        <td className="td font-mono">{t.tradingview_raw_symbol}</td>
                        <td className="td font-mono">{t.canonical_symbol}</td>
                        <td className="td font-mono">{t.shark_symbol}</td>
                        <td className="td font-semibold">{t.side}</td>
                        <td className="td font-mono">{fmtPrice(t.entry_price)}</td>
                        <td className="td font-mono">{fmtPrice(t.exit_price)}</td>
                        <td className="td font-mono">{fmtNum(t.quantity, 4)}</td>
                        <td className="td font-mono">{fmtNum(t.margin)}</td>
                        <td className="td">{t.leverage}x</td>
                        <td className="td font-mono">{fmtNum(t.entry_fee, 4)}</td>
                        <td className="td font-mono">{fmtNum(t.exit_fee, 4)}</td>
                        <td className="td font-mono">{fmtNum(t.funding, 4)}</td>
                        <td className={`td font-mono ${pnlTone(t.gross_pnl)}`}>{fmtPnl(t.gross_pnl)}</td>
                        <td className={`td font-mono font-semibold ${pnlTone(t.net_pnl)}`}>{fmtPnl(t.net_pnl)}</td>
                        <td className={`td font-mono font-semibold ${pnlTone(t.roi_percent)}`}>{fmtNum(t.roi_percent)}%</td>
                        <td className={`td font-mono ${pnlTone(t.price_change_percent)}`}>{fmtNum(t.price_change_percent)}%</td>
                        <td className="td font-mono text-muted">{Math.floor(t.duration_seconds / 60)}m</td>
                        <td className="td"><StatusBadge status={t.mode} /></td>
                        <td className="td"><span className={`badge ${t.result === "WIN" ? "badge-green" : t.result === "LOSS" ? "badge-red" : "badge-neutral"}`}>{t.result}</span></td>
                        <td className="td font-mono text-[10px] text-muted">{t.event_id}</td>
                        <td className="td font-mono text-[10px] text-muted">{t.exit_order_id}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}

        {tab === "orders" && (
          (orders.data ?? []).length === 0 ? <EmptyState message="No orders." /> : (
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-border">
                    {["Created", "Order ID", "TV Symbol", "Canonical", "Shark", "Side", "Type", "Entry/Exit", "Reduce", "Qty", "Fill Price", "Filled", "Status", "Payload", "Error"].map((h) => (
                      <th key={h} className="th px-2 py-2">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(orders.data ?? []).map((o) => (
                    <tr key={o.order_id} className="border-b border-border/50 row-hover">
                      <td className="td font-mono text-muted">{fmtTime(o.created_at)}</td>
                      <td className="td font-mono text-[10.5px]">{o.order_id}</td>
                      <td className="td font-mono">{o.tradingview_raw_symbol}</td>
                      <td className="td font-mono">{o.canonical_symbol}</td>
                      <td className="td font-mono font-semibold">{o.shark_symbol}</td>
                      <td className="td">{o.side}</td>
                      <td className="td">{o.order_type}</td>
                      <td className="td">{o.entry_exit}</td>
                      <td className="td">{o.reduce_only ? "YES" : "NO"}</td>
                      <td className="td font-mono">{fmtNum(o.quantity, 4)}</td>
                      <td className="td font-mono">{fmtPrice(o.avg_fill_price)}</td>
                      <td className="td font-mono">{fmtNum(o.filled_quantity, 4)}</td>
                      <td className="td"><StatusBadge status={o.status} /></td>
                      <td className="td">
                        <details>
                          <summary className="cursor-pointer text-[10.5px] text-info">view</summary>
                          <pre className="text-[10px] font-mono text-muted">{JSON.stringify(o.submitted_payload, null, 1)}</pre>
                        </details>
                      </td>
                      <td className="td text-loss text-[10.5px]">{o.error_code || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}
      </Panel>
    </div>
  );
}
