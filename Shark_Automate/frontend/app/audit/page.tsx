"use client";

import { fmtTime, usePolling } from "@/lib/api";
import { Panel, StatusBadge, EmptyState, ErrorState } from "@/components/ui";

interface AuditRow { action: string; actor: string; entity: string; entity_id: string; detail: unknown; correlation_id: string; ts: string }
interface SystemRow { event: string; level: string; message: string; detail: unknown; correlation_id: string; mode: string; ts: string }
interface ReconRow { severity: string; kind: string; message: string; internal_state: unknown; exchange_state: unknown; resolved: boolean; ts: string }

export default function AuditPage() {
  const audit = usePolling<AuditRow[]>("/api/audit", 5000);
  const events = usePolling<SystemRow[]>("/api/events", 5000);
  const recon = usePolling<ReconRow[]>("/api/reconciliation", 8000);

  return (
    <div className="space-y-4">
      <Panel title="Reconciliation Events">
        {(recon.data ?? []).length === 0 ? (
          <EmptyState message="No reconciliation mismatches. Internal state matches the exchange." />
        ) : (
          <table className="w-full text-left">
            <thead><tr className="border-b border-border">{["Time", "Severity", "Kind", "Message", "Internal", "Exchange"].map((h) => <th key={h} className="th px-2 py-1">{h}</th>)}</tr></thead>
            <tbody>
              {(recon.data ?? []).map((r, i) => (
                <tr key={i} className="border-b border-border/50">
                  <td className="td font-mono text-muted">{fmtTime(r.ts)}</td>
                  <td className="td"><span className="badge badge-red">{r.severity}</span></td>
                  <td className="td font-semibold">{r.kind}</td>
                  <td className="td">{r.message}</td>
                  <td className="td font-mono text-[10px] text-muted">{JSON.stringify(r.internal_state)}</td>
                  <td className="td font-mono text-[10px] text-muted">{JSON.stringify(r.exchange_state)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <Panel title="System Events">
          {events.error && <ErrorState message={events.error} />}
          {(events.data ?? []).length === 0 ? <EmptyState message="No system events." /> : (
            <div className="space-y-1 max-h-[480px] overflow-y-auto">
              {(events.data ?? []).map((e, i) => (
                <div key={i} className="border-b border-border/50 py-1 text-[12px]">
                  <div className="flex gap-2 items-center">
                    <span className="font-mono text-[10.5px] text-muted">{fmtTime(e.ts)}</span>
                    <span className="font-semibold">{e.event}</span>
                    <StatusBadge status={e.mode} />
                    <span className="text-[10.5px] text-muted font-mono">{e.correlation_id}</span>
                  </div>
                  <div className="text-muted text-[11px]">{e.message}</div>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Audit Log">
          {audit.error && <ErrorState message={audit.error} />}
          {(audit.data ?? []).length === 0 ? <EmptyState message="No audit entries." /> : (
            <div className="space-y-1 max-h-[480px] overflow-y-auto">
              {(audit.data ?? []).map((a, i) => (
                <div key={i} className="border-b border-border/50 py-1 text-[12px]">
                  <div className="flex gap-2 items-center">
                    <span className="font-mono text-[10.5px] text-muted">{fmtTime(a.ts)}</span>
                    <span className="font-semibold">{a.action}</span>
                    <span className="text-muted">{a.actor} · {a.entity}</span>
                    <span className="text-[10.5px] text-muted font-mono">{a.correlation_id}</span>
                  </div>
                  <div className="text-muted text-[10.5px] font-mono">{JSON.stringify(a.detail)}</div>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
