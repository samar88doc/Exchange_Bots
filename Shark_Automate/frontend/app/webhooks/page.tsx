"use client";

import { fmtNum, fmtTime, usePolling } from "@/lib/api";
import { Panel, Stat, StatusBadge, EmptyState, ErrorState } from "@/components/ui";

interface WebhookMonitor {
  health: string;
  last_event: string | null;
  last_event_ts: string | null;
  events_per_minute: number;
  successful_signals: number;
  rejected_signals: number;
  duplicate_signals: number;
  invalid_signals: number;
  failed_events: number;
  normalization_failures: number;
  recent: {
    ts: string;
    event_id: string;
    status: string;
    reason: string;
    payload: Record<string, unknown>;
    latency_ms: number | null;
  }[];
}

export default function WebhooksPage() {
  const { data, error } = usePolling<WebhookMonitor>("/api/webhooks/monitor", 3000);

  return (
    <div className="space-y-4">
      <Panel title="TradingView Webhook Health" right={<StatusBadge status={data?.health ?? "LOADING"} />}>
        {error && <ErrorState message={error} />}
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-2">
          <Stat label="Events / min" value={fmtNum(data?.events_per_minute)} />
          <Stat label="Successful" value={data?.successful_signals ?? 0} tone="text-profit" />
          <Stat label="Rejected" value={data?.rejected_signals ?? 0} tone="text-loss" />
          <Stat label="Duplicates" value={data?.duplicate_signals ?? 0} tone="text-warn" />
          <Stat label="Invalid" value={data?.invalid_signals ?? 0} tone="text-loss" />
          <Stat label="Failed" value={data?.failed_events ?? 0} tone="text-loss" />
          <Stat label="Norm. Failures" value={data?.normalization_failures ?? 0} tone="text-warn" />
          <Stat label="Last Event" value={data?.last_event ? fmtTime(data.last_event_ts).slice(-8) : "—"} />
        </div>
      </Panel>

      <Panel title="Real-time Event Feed">
        {(data?.recent ?? []).length === 0 ? (
          <EmptyState message="No webhook events yet. Point your TradingView alert at POST /api/webhooks/tradingview." />
        ) : (
          <div className="space-y-1.5">
            {(data?.recent ?? []).map((e) => {
              const p = (e.payload ?? {}) as { symbol?: string; action?: string; strategy_id?: string };
              return (
                <div key={e.event_id + e.ts} className="flex items-start gap-3 border-b border-border/50 py-1.5">
                  <span className="font-mono text-[11.5px] text-muted w-20 shrink-0">{fmtTime(e.ts).slice(-8)}</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-[12.5px]">
                      <span className="font-semibold">{p.strategy_id || "strategy"}</span>{" "}
                      <span className="font-mono">{p.symbol}</span>{" "}
                      <span className="font-semibold">{p.action}</span>
                    </div>
                    <div className="text-[10.5px] text-muted font-mono truncate">{e.event_id}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    {e.latency_ms !== null && <span className="text-[10.5px] text-muted font-mono">{e.latency_ms}ms</span>}
                    <StatusBadge status={e.status} />
                    {e.reason && <span className="text-[10.5px] text-warn">{e.reason}</span>}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
