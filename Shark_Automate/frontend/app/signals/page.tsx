"use client";

import { useState } from "react";
import { fmtTime, usePolling } from "@/lib/api";
import type { SignalRow } from "@/lib/types";
import { Panel, SymTriple, StatusBadge, EmptyState, ErrorState } from "@/components/ui";

interface TimelineEvent {
  event: string;
  from_state: string;
  to_state: string;
  detail: Record<string, unknown>;
  ts: string;
}

export default function SignalsPage() {
  const { data, error, loading } = usePolling<SignalRow[]>("/api/signals?limit=200", 3000);
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [selected, setSelected] = useState<SignalRow | null>(null);
  const [detail, setDetail] = useState<{ signal: SignalRow; timeline: TimelineEvent[] } | null>(null);

  const rows = (data ?? []).filter((s) => {
    if (statusFilter && s.status_display !== statusFilter) return false;
    if (!filter) return true;
    const q = filter.toUpperCase();
    return (
      s.tradingview_raw_symbol.includes(q) ||
      s.canonical_symbol.includes(q) ||
      s.shark_symbol.includes(q) ||
      s.event_id.toUpperCase().includes(q) ||
      s.strategy_id.toUpperCase().includes(q)
    );
  });

  async function openDetail(row: SignalRow) {
    setSelected(row);
    const res = await fetch(`/api/signals/${row.id}`);
    setDetail(await res.json());
  }

  return (
    <div className="space-y-4">
      <Panel
        title="TradingView Signal History"
        right={
          <div className="flex gap-2">
            <input className="input w-48" placeholder="Filter symbol / event / strategy…" value={filter} onChange={(e) => setFilter(e.target.value)} />
            <select className="input" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">All statuses</option>
              {["RECEIVED", "VALIDATED", "ACCEPTED", "EXECUTED", "REJECTED", "DUPLICATE", "MAX_POSITIONS", "SYMBOL_ALREADY_OPEN", "SHARK_SYMBOL_NOT_FOUND", "INVALID", "ERROR"].map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
        }
      >
        {error && <ErrorState message={error} />}
        {!error && rows.length === 0 ? (
          <EmptyState message={loading ? "Loading…" : "No signals match the filter."} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-border">
                  {["Time", "Strategy", "TradingView Symbol", "Canonical", "Shark", "Action", "Source", "Event ID", "Status", "Reason", "Position", "Order", "Mode"].map((h) => (
                    <th key={h} className="th px-3 py-2">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => (
                  <tr key={s.id} className="border-b border-border/50 row-hover cursor-pointer" onClick={() => openDetail(s)}>
                    <td className="td font-mono text-muted">{fmtTime(s.received_at)}</td>
                    <td className="td">{s.strategy_name || s.strategy_id}</td>
                    <td className="td font-mono">{s.tradingview_raw_symbol}</td>
                    <td className="td font-mono">{s.canonical_symbol}</td>
                    <td className="td font-mono">{s.shark_symbol || "—"}</td>
                    <td className="td font-semibold">{s.action}</td>
                    <td className="td">{s.source}</td>
                    <td className="td font-mono text-muted text-[10.5px]">{s.event_id}</td>
                    <td className="td"><StatusBadge status={s.status_display} /></td>
                    <td className="td text-muted">{s.rejection_reason || "—"}</td>
                    <td className="td font-mono text-[10.5px]">{s.position_id ?? "—"}</td>
                    <td className="td font-mono text-[10.5px]">{s.order_id ?? "—"}</td>
                    <td className="td"><StatusBadge status={s.mode} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {selected && (
        <Panel
          title={`Event Timeline · ${selected.event_id}`}
          right={<button className="btn" onClick={() => { setSelected(null); setDetail(null); }}>Close</button>}
        >
          <SymTriple raw={selected.tradingview_raw_symbol} canonical={selected.canonical_symbol} shark={selected.shark_symbol} />
          <ol className="mt-3 space-y-1.5 border-l-2 border-border2 pl-4">
            {(detail?.timeline ?? []).map((t, i) => (
              <li key={i} className="relative">
                <span className="absolute -left-[21px] top-1.5 w-2.5 h-2.5 rounded-full bg-info" />
                <div className="flex gap-3 items-baseline">
                  <span className="font-mono text-[11px] text-muted">{fmtTime(t.ts)}</span>
                  <span className="text-xs font-semibold">{t.event.replace(/_/g, " ")}</span>
                  {t.to_state && (
                    <span className="text-[11px] text-muted">
                      {t.from_state && <>{t.from_state} → </>}
                      {t.to_state}
                    </span>
                  )}
                </div>
                {Object.keys(t.detail ?? {}).length > 0 && (
                  <pre className="text-[10.5px] text-muted font-mono mt-0.5">{JSON.stringify(t.detail, null, 1)}</pre>
                )}
              </li>
            ))}
            {(detail?.timeline ?? []).length === 0 && <EmptyState message="No timeline events yet." />}
          </ol>
        </Panel>
      )}
    </div>
  );
}
