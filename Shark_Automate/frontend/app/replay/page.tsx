"use client";

import { useState } from "react";
import { apiSend } from "@/lib/api";
import { Panel, EmptyState } from "@/components/ui";

interface ReplayResult {
  total: number;
  executed: number;
  rejected: number;
  details: { event_id: string; status: string; reason: string; position_id?: string }[];
}

export default function ReplayPage() {
  const [form, setForm] = useState({ start: "", end: "", strategy_id: "", symbol: "", speed: "0" });
  const [result, setResult] = useState<ReplayResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function run() {
    setBusy(true);
    setError("");
    try {
      const body: Record<string, unknown> = { speed: parseFloat(form.speed) || 0 };
      if (form.start) body.start = new Date(form.start).toISOString();
      if (form.end) body.end = new Date(form.end).toISOString();
      if (form.strategy_id) body.strategy_id = form.strategy_id;
      if (form.symbol) body.symbol = form.symbol;
      setResult(await apiSend<ReplayResult>("/api/replay", "POST", body));
    } catch (e) {
      setError(e instanceof Error ? e.message : "replay failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Panel title="Signal Replay Tool (always PAPER)">
        <p className="text-[11.5px] text-muted mb-3">
          Replays stored TradingView webhook events through the trading engine — useful for reproducing bugs and
          validating logic changes. Filters: timestamp range, strategy, symbol (raw / canonical / shark). Replay speed:
          0 = as fast as possible, 1 = realtime spacing.
        </p>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2 items-end">
          <label className="text-[11px] text-muted">Start
            <input type="datetime-local" className="input w-full" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} />
          </label>
          <label className="text-[11px] text-muted">End
            <input type="datetime-local" className="input w-full" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} />
          </label>
          <label className="text-[11px] text-muted">Strategy
            <select className="input w-full" value={form.strategy_id} onChange={(e) => setForm({ ...form, strategy_id: e.target.value })}>
              <option value="">All</option>
              <option value="strategy_1">Strategy 1</option>
              <option value="strategy_2">Strategy 2</option>
            </select>
          </label>
          <label className="text-[11px] text-muted">Symbol (raw/canonical/shark)
            <input className="input w-full" placeholder="e.g. BASUSDT.P" value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} />
          </label>
          <div className="flex gap-2">
            <label className="text-[11px] text-muted w-20">Speed
              <input className="input w-full" value={form.speed} onChange={(e) => setForm({ ...form, speed: e.target.value })} />
            </label>
            <button className="btn btn-ok h-8" disabled={busy} onClick={run}>{busy ? "Running…" : "Replay"}</button>
          </div>
        </div>
        {error && <div className="mt-3 text-loss text-xs">{error}</div>}
      </Panel>

      <Panel title="Replay Report">
        {!result ? (
          <EmptyState message="No replay run yet." />
        ) : (
          <>
            <div className="flex gap-4 text-sm mb-3">
              <span>Total: <b className="font-mono">{result.total}</b></span>
              <span className="text-profit">Executed: <b className="font-mono">{result.executed}</b></span>
              <span className="text-loss">Rejected: <b className="font-mono">{result.rejected}</b></span>
            </div>
            <table className="w-full text-left">
              <thead><tr className="border-b border-border">{["Event ID", "Status", "Reason", "Position"].map((h) => <th key={h} className="th px-2 py-1">{h}</th>)}</tr></thead>
              <tbody>
                {result.details.map((d) => (
                  <tr key={d.event_id} className="border-b border-border/50">
                    <td className="td font-mono text-[10.5px]">{d.event_id}</td>
                    <td className="td">{d.status}</td>
                    <td className="td text-warn">{d.reason || "—"}</td>
                    <td className="td font-mono text-[10.5px]">{d.position_id ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </Panel>
    </div>
  );
}
