"use client";

import { useState } from "react";
import { fmtNum, fmtPnl, fmtPrice, fmtTime, usePolling } from "@/lib/api";
import type { PositionRow } from "@/lib/types";
import { Panel, SymTriple, StatusBadge, EmptyState, ErrorState, pnlTone, Stat } from "@/components/ui";

export default function PositionsPage() {
  const active = usePolling<PositionRow[]>("/api/positions", 3000);
  const history = usePolling<PositionRow[]>("/api/positions?active_only=false", 5000);
  const [showAll, setShowAll] = useState(false);
  const rows = showAll ? history.data ?? [] : active.data ?? [];

  async function closePosition(id: string) {
    const ok = window.confirm(`Close position ${id}? This submits a reduce-only market close.`);
    if (!ok) return;
    await fetch(`/api/positions/${id}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "MANUAL", confirm: true }),
    });
    active.refresh();
    history.refresh();
  }

  async function closeAll() {
    const ok = window.confirm("CLOSE ALL open positions?");
    if (!ok) return;
    await fetch("/api/positions/close-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "MANUAL", confirm: true }),
    });
    active.refresh();
    history.refresh();
  }

  return (
    <div className="space-y-4">
      <Panel
        title="Positions"
        right={
          <div className="flex gap-2 items-center">
            <label className="text-[11px] text-muted flex items-center gap-1">
              <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> show closed
            </label>
            <button className="btn btn-danger" onClick={closeAll}>Close All</button>
          </div>
        }
      >
        {active.error && <ErrorState message={active.error} />}
        {rows.length === 0 ? (
          <EmptyState message="No positions." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-border">
                  {["Symbols (TV / Canonical / Shark)", "Exchange", "Direction", "Entry", "Current", "Qty", "Margin (USDT)", "Notional (USDT)", "Lev", "uPnL (USDT)", "ROI", "TP", "Opened", "State", "Actions"].map((h) => (
                    <th key={h} className="th px-3 py-2">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.position_id} className="border-b border-border/50 row-hover">
                    <td className="td"><SymTriple raw={p.tradingview_raw_symbol} canonical={p.canonical_symbol} shark={p.shark_symbol} /></td>
                    <td className="td">{p.exchange} <span className="text-[10px] text-muted">({p.mode})</span></td>
                    <td className="td font-semibold">{p.direction}</td>
                    <td className="td font-mono">{fmtPrice(p.entry_price)}</td>
                    <td className="td font-mono">{fmtPrice(p.mark_price)}</td>
                    <td className="td font-mono">{fmtNum(p.quantity, 4)}</td>
                    <td className="td font-mono">{fmtNum(p.margin)}</td>
                    <td className="td font-mono">{fmtNum(p.notional)}</td>
                    <td className="td">{p.leverage}x</td>
                    <td className={`td font-mono font-semibold ${pnlTone(p.unrealized_pnl)}`}>{fmtPnl(p.unrealized_pnl)}</td>
                    <td className={`td font-mono font-semibold ${pnlTone(p.roi_percent)}`}>{fmtNum(p.roi_percent)}%</td>
                    <td className="td font-mono text-profit">{fmtPrice(p.target_price)}</td>
                    <td className="td font-mono text-muted">{fmtTime(p.open_time)}</td>
                    <td className="td"><StatusBadge status={p.state} /></td>
                    <td className="td">
                      {!["CLOSED", "REJECTED", "CANCELLED"].includes(p.state) && (
                        <button className="btn btn-danger" onClick={() => closePosition(p.position_id)}>Close</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
