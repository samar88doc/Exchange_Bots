"use client";

import { useState } from "react";
import { apiSend, fmtNum, usePolling } from "@/lib/api";
import { Panel, EmptyState, ErrorState, StatusBadge } from "@/components/ui";

interface SettingsView {
  trading: Record<string, unknown>;
  execution: Record<string, unknown>;
  paper_trading: Record<string, unknown>;
  exchange: Record<string, unknown>;
  symbol_mapping: Record<string, unknown>;
  webhook: Record<string, unknown>;
  system: Record<string, unknown>;
  live_gate: { live_trading_enabled: boolean; live_capable: boolean; confirmation_phrase: string };
  runtime: Record<string, string>;
}

interface PaperReport { [k: string]: unknown }

interface MappingRow {
  id: number; tradingview_raw_symbol: string; canonical_symbol: string; shark_symbol: string;
  contract_status: string; quantity_precision: number; price_precision: number;
  minimum_quantity: string; minimum_notional: string; max_leverage: number | null;
  verified_against_exchange: boolean;
}

export default function SettingsPage() {
  const settings = usePolling<SettingsView>("/api/settings", 6000);
  const mappings = usePolling<MappingRow[]>("/api/symbols/mappings", 10000);
  const [report, setReport] = useState<PaperReport | null>(null);
  const [liveAck, setLiveAck] = useState({ phrase: "", secret: "", margin: "50", leverage: "10", maxPos: "3", roi: "10", sl: false });
  const [liveMsg, setLiveMsg] = useState("");
  const [syncMsg, setSyncMsg] = useState("");

  const s = settings.data;

  async function loadReport() {
    setReport(await apiSend<PaperReport>("/api/live/paper-report", "GET"));
  }

  async function syncSymbols() {
    try {
      const r = await apiSend<{ created: number; updated: number }>("/api/symbols/sync", "POST");
      setSyncMsg(`Synced from Shark exchangeInfo (verified): +${r.created} created, ${r.updated} updated`);
      mappings.refresh();
    } catch (e) {
      setSyncMsg(e instanceof Error ? e.message : "sync failed");
    }
  }

  async function goLive() {
    setLiveMsg("");
    try {
      const r = await apiSend<{ mode: string; dry_run: boolean; warning: string }>("/api/live/activate", "POST", {
        confirmation_phrase: liveAck.phrase,
        secret_confirmation: liveAck.secret,
        acknowledge_margin_per_trade: parseFloat(liveAck.margin),
        acknowledge_leverage: parseInt(liveAck.leverage),
        acknowledge_max_positions: parseInt(liveAck.maxPos),
        acknowledge_target_roi: parseFloat(liveAck.roi),
        acknowledge_stop_loss_disabled: liveAck.sl,
      });
      setLiveMsg(`✅ LIVE ${r.dry_run ? "(DRY RUN)" : "ACTIVE"} — ${r.warning}`);
      settings.refresh();
    } catch (e) {
      setLiveMsg(`❌ ${e instanceof Error ? e.message : "activation failed"}`);
    }
  }

  async function backToPaper() {
    await apiSend("/api/live/deactivate", "POST");
    settings.refresh();
  }

  return (
    <div className="space-y-4">
      {settings.error && <ErrorState message={settings.error} />}

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Panel title="TRADING">
          <dl className="text-xs space-y-1.5">
            {Object.entries(s?.trading ?? {}).map(([k, v]) => (
              <div key={k} className="flex justify-between border-b border-border/50 py-1">
                <dt className="text-muted">{k.replace(/_/g, " ")}</dt>
                <dd className="font-mono">{String(v)}</dd>
              </div>
            ))}
          </dl>
        </Panel>
        <Panel title="EXECUTION / PAPER TRADING">
          <dl className="text-xs space-y-1.5">
            {[...(Object.entries(s?.execution ?? [])), ...(Object.entries(s?.paper_trading ?? {}))].map(([k, v]) => (
              <div key={k} className="flex justify-between border-b border-border/50 py-1">
                <dt className="text-muted">{k.replace(/_/g, " ")}</dt>
                <dd className="font-mono">{String(v)}</dd>
              </div>
            ))}
          </dl>
        </Panel>
        <Panel title="EXCHANGE / SYMBOL MAPPING / WEBHOOK / SYSTEM">
          <dl className="text-xs space-y-1.5">
            {[...(Object.entries(s?.exchange ?? {})), ...(Object.entries(s?.symbol_mapping ?? {})), ...(Object.entries(s?.webhook ?? {})), ...(Object.entries(s?.system ?? {}))].map(([k, v]) => (
              <div key={k} className="flex justify-between border-b border-border/50 py-1">
                <dt className="text-muted">{k.replace(/_/g, " ")}</dt>
                <dd className="font-mono truncate ml-2">{String(v)}</dd>
              </div>
            ))}
          </dl>
        </Panel>
      </div>

      <Panel title="Symbol Mapping (TradingView → Canonical → Shark)">
        <div className="flex items-center justify-between mb-2">
          <p className="text-[11px] text-muted">
            Normalization: {String(s?.symbol_mapping?.normalization_enabled)} · suffix “{String(s?.symbol_mapping?.tradingview_perpetual_suffix)}” · contracts synced from verified Shark exchangeInfo.
          </p>
          <button className="btn" onClick={syncSymbols}>Sync from Shark exchangeInfo</button>
        </div>
        {syncMsg && <div className="text-[11px] text-info mb-2">{syncMsg}</div>}
        <div className="overflow-x-auto max-h-72 overflow-y-auto">
          <table className="w-full text-left">
            <thead><tr className="border-b border-border">
              {["TV Raw", "Canonical", "Shark", "Status", "Qty Prec", "Price Prec", "Min Qty", "Min Notional", "Max Lev", "Verified"].map((h) => <th key={h} className="th px-2 py-1">{h}</th>)}
            </tr></thead>
            <tbody>
              {(mappings.data ?? []).map((m) => (
                <tr key={m.id} className="border-b border-border/50">
                  <td className="td font-mono">{m.tradingview_raw_symbol}</td>
                  <td className="td font-mono font-semibold">{m.canonical_symbol}</td>
                  <td className="td font-mono font-semibold">{m.shark_symbol}</td>
                  <td className="td"><StatusBadge status={m.contract_status} /></td>
                  <td className="td">{m.quantity_precision}</td>
                  <td className="td">{m.price_precision}</td>
                  <td className="td font-mono">{fmtNum(m.minimum_quantity, 6)}</td>
                  <td className="td font-mono">{fmtNum(m.minimum_notional)}</td>
                  <td className="td">{m.max_leverage ?? "—"}</td>
                  <td className="td">{m.verified_against_exchange ? "YES" : "manual"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {/* GO LIVE WORKFLOW */}
      <Panel title="Paper Test Status & GO LIVE">
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <div>
            <button className="btn mb-3" onClick={loadReport}>Generate Paper Test Report</button>
            {report ? (
              <dl className="text-xs space-y-1 max-h-80 overflow-y-auto">
                {Object.entries(report).filter(([k]) => k !== "charts").map(([k, v]) => (
                  <div key={k} className="flex justify-between border-b border-border/50 py-1">
                    <dt className="text-muted">{k.replace(/_/g, " ")}</dt>
                    <dd className="font-mono">{String(v)}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <EmptyState message="Run at least 2 full trading days of PAPER mode, then review the report here before considering LIVE." />
            )}
          </div>

          <div className="border border-loss/40 rounded p-3 bg-loss/5">
            <div className="th mb-2 text-loss">GO LIVE — MANUAL ACTIVATION</div>
            <p className="text-[11px] text-muted mb-2">
              Final warning — trading with <b>margin {liveAck.margin} USDT</b>, <b>{liveAck.leverage}x leverage</b>,{" "}
              <b>max {liveAck.maxPos} positions</b>, <b>target ROI {liveAck.roi}%</b>,{" "}
              <b className="text-warn">STOP LOSS = DISABLED</b>, exchange: Shark ({String(s?.exchange?.base_url)}),{" "}
              margin asset {String(s?.exchange?.margin_asset)}. Symbol normalization always applies.
              {!s?.live_gate.live_trading_enabled && (
                <span className="block mt-1 text-warn font-semibold">
                  LIVE_TRADING_ENABLED is false in the environment — set it in .env.live and restart first.
                </span>
              )}
            </p>
            <div className="grid grid-cols-2 gap-2 mb-2">
              <label className="text-[11px] text-muted">Margin (USDT)<input className="input w-full" value={liveAck.margin} onChange={(e) => setLiveAck({ ...liveAck, margin: e.target.value })} /></label>
              <label className="text-[11px] text-muted">Leverage<input className="input w-full" value={liveAck.leverage} onChange={(e) => setLiveAck({ ...liveAck, leverage: e.target.value })} /></label>
              <label className="text-[11px] text-muted">Max Positions<input className="input w-full" value={liveAck.maxPos} onChange={(e) => setLiveAck({ ...liveAck, maxPos: e.target.value })} /></label>
              <label className="text-[11px] text-muted">Target ROI %<input className="input w-full" value={liveAck.roi} onChange={(e) => setLiveAck({ ...liveAck, roi: e.target.value })} /></label>
            </div>
            <label className="text-[11px] text-muted block mb-2">
              Confirmation phrase (exact): <b>{s?.live_gate.confirmation_phrase || "ENABLE LIVE TRADING"}</b>
              <input className="input w-full mt-1" placeholder={s?.live_gate.confirmation_phrase || "ENABLE LIVE TRADING"} value={liveAck.phrase} onChange={(e) => setLiveAck({ ...liveAck, phrase: e.target.value })} />
            </label>
            <label className="text-[11px] text-muted block mb-2">
              LIVE_TRADING_CONFIRMATION secret (if configured)
              <input className="input w-full mt-1" type="password" value={liveAck.secret} onChange={(e) => setLiveAck({ ...liveAck, secret: e.target.value })} />
            </label>
            <label className="flex items-center gap-2 text-[11px] mb-3">
              <input type="checkbox" checked={liveAck.sl} onChange={(e) => setLiveAck({ ...liveAck, sl: e.target.checked })} />
              I acknowledge <b className="text-warn">STOP LOSS is DISABLED</b>
            </label>
            <div className="flex gap-2">
              <button className="btn btn-danger flex-1" onClick={goLive} disabled={!s?.live_gate.live_capable && !s?.live_gate.live_trading_enabled}>
                ACTIVATE LIVE TRADING
              </button>
              <button className="btn flex-1" onClick={backToPaper}>Back to PAPER</button>
            </div>
            {liveMsg && <div className="mt-2 text-xs">{liveMsg}</div>}
          </div>
        </div>
      </Panel>
    </div>
  );
}
