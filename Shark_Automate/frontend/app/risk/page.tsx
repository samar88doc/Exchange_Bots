"use client";

import { useState } from "react";
import { apiSend, fmtNum, fmtTime, usePolling } from "@/lib/api";
import { Panel, Stat, StatusBadge, EmptyState, ErrorState } from "@/components/ui";

interface RiskCenter {
  max_open_positions: number;
  open_positions: number;
  available_slots: number;
  reservations: { canonical_symbol: string; position_id: string; status: string; created_at: string }[];
  margin_per_trade: string;
  leverage: number;
  target_roi_percent: string;
  stop_loss_enabled: boolean;
  duplicate_protection: string;
  symbol_normalization: string;
  trading_halted: boolean;
  kill_switch: boolean;
  daily_trade_limit: number;
  daily_loss_limit: string;
  recent_events: { event: string; reason: string; detail: unknown; ts: string }[];
}

export default function RiskPage() {
  const { data, error, refresh } = usePolling<RiskCenter>("/api/risk", 3000);
  const [confirmResume, setConfirmResume] = useState(false);

  async function halt() {
    if (!window.confirm("HALT trading? New entries will be rejected. Existing positions keep being monitored.")) return;
    await apiSend("/api/risk/halt", "POST", { reason: "OPERATOR", confirm: true });
    refresh();
  }
  async function resume() {
    await apiSend("/api/risk/resume", "POST", { reason: "OPERATOR", confirm: true });
    setConfirmResume(false);
    refresh();
  }
  async function kill(on: boolean) {
    if (on && !window.confirm("ENABLE KILL SWITCH? All new entries rejected immediately.")) return;
    await apiSend("/api/risk/kill-switch", "POST", { reason: "OPERATOR", confirm: on });
    refresh();
  }

  return (
    <div className="space-y-4">
      {(data?.trading_halted || data?.kill_switch) && (
        <div className="rounded border border-warn bg-warn/10 px-4 py-3 text-warn font-bold tracking-widest text-center animate-pulse">
          ⚠ TRADING HALTED — signals are logged but NO new orders are placed
        </div>
      )}
      {error && <ErrorState message={error} />}

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Panel title="Position Limits">
          <div className="grid grid-cols-2 gap-2">
            <Stat label="Maximum Open" value={data?.max_open_positions ?? 3} />
            <Stat label="Current Open" value={data?.open_positions ?? 0} />
            <Stat label="Available Slots" value={data?.available_slots ?? 0} />
            <Stat label="Current Reservations" value={data?.reservations.length ?? 0} tone="text-warn" />
          </div>
          <div className="mt-3 space-y-1 text-xs">
            <div className="flex justify-between border-b border-border/50 py-1"><span className="text-muted">Margin / Trade</span><span className="font-mono">{fmtNum(data?.margin_per_trade)} USDT</span></div>
            <div className="flex justify-between border-b border-border/50 py-1"><span className="text-muted">Leverage</span><span className="font-mono">{data?.leverage}x</span></div>
            <div className="flex justify-between border-b border-border/50 py-1"><span className="text-muted">Target ROI</span><span className="font-mono text-profit">{fmtNum(data?.target_roi_percent)}%</span></div>
            <div className="flex justify-between border-b border-border/50 py-1"><span className="text-muted">Stop Loss</span><span className="font-mono text-warn">{data?.stop_loss_enabled ? "ON" : "DISABLED"}</span></div>
            <div className="flex justify-between border-b border-border/50 py-1"><span className="text-muted">Duplicate Protection</span><span className="font-mono text-profit">{data?.duplicate_protection}</span></div>
            <div className="flex justify-between border-b border-border/50 py-1"><span className="text-muted">Symbol Normalization</span><span className="font-mono text-profit">{data?.symbol_normalization}</span></div>
            <div className="flex justify-between border-b border-border/50 py-1"><span className="text-muted">Daily Trade Limit</span><span className="font-mono">{data?.daily_trade_limit || "unlimited"}</span></div>
            <div className="flex justify-between py-1"><span className="text-muted">Daily Loss Limit</span><span className="font-mono">{parseFloat(data?.daily_loss_limit || "0") ? fmtNum(data?.daily_loss_limit) : "unlimited"}</span></div>
          </div>
        </Panel>

        <Panel title="Emergency Controls">
          <div className="space-y-3">
            <div className="panel2 bg-panel2 border border-border rounded p-3">
              <div className="th mb-1">KILL SWITCH</div>
              <p className="text-[11px] text-muted mb-2">
                Rejects all new entries instantly. Existing positions keep being monitored; manual closes stay available.
              </p>
              {data?.kill_switch ? (
                <button className="btn btn-ok w-full" onClick={() => kill(false)}>Disable Kill Switch</button>
              ) : (
                <button className="btn btn-danger w-full" onClick={() => kill(true)}>ENABLE KILL SWITCH</button>
              )}
            </div>
            <div className="panel2 bg-panel2 border border-border rounded p-3">
              <div className="th mb-1">TRADING HALT</div>
              <p className="text-[11px] text-muted mb-2">
                TradingView signals continue to be received and logged, but MUST NOT place new orders. Resuming requires confirmation.
              </p>
              {data?.trading_halted ? (
                confirmResume ? (
                  <div className="space-y-2">
                    <p className="text-[11px] text-warn">Confirm: resume placing orders?</p>
                    <div className="flex gap-2">
                      <button className="btn btn-warn flex-1" onClick={resume}>Yes, RESUME</button>
                      <button className="btn flex-1" onClick={() => setConfirmResume(false)}>Cancel</button>
                    </div>
                  </div>
                ) : (
                  <button className="btn btn-warn w-full" onClick={() => setConfirmResume(true)}>Resume Trading…</button>
                )
              ) : (
                <button className="btn btn-danger w-full" onClick={halt}>Pause / Halt Trading</button>
              )}
            </div>
          </div>
        </Panel>

        <Panel title="Active Reservations">
          {(data?.reservations ?? []).length === 0 ? (
            <EmptyState message="No active reservations." />
          ) : (
            <table className="w-full text-left">
              <thead><tr className="border-b border-border">{["Symbol", "Position", "Status", "At"].map((h) => <th key={h} className="th px-2 py-1">{h}</th>)}</tr></thead>
              <tbody>
                {(data?.reservations ?? []).map((r) => (
                  <tr key={r.position_id} className="border-b border-border/50">
                    <td className="td font-mono">{r.canonical_symbol}</td>
                    <td className="td font-mono text-[10.5px]">{r.position_id}</td>
                    <td className="td"><StatusBadge status={r.status} /></td>
                    <td className="td font-mono text-muted">{fmtTime(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>

      <Panel title="Recent Risk Events">
        {(data?.recent_events ?? []).length === 0 ? (
          <EmptyState message="No risk events." />
        ) : (
          <table className="w-full text-left">
            <thead><tr className="border-b border-border">{["Time", "Event", "Reason", "Detail"].map((h) => <th key={h} className="th px-2 py-1">{h}</th>)}</tr></thead>
            <tbody>
              {(data?.recent_events ?? []).map((e, i) => (
                <tr key={i} className="border-b border-border/50">
                  <td className="td font-mono text-muted">{fmtTime(e.ts)}</td>
                  <td className="td font-semibold">{e.event}</td>
                  <td className="td text-warn">{e.reason}</td>
                  <td className="td text-muted text-[10.5px] font-mono">{JSON.stringify(e.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
