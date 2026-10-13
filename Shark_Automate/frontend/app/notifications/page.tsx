"use client";

import { usePolling } from "@/lib/api";
import { Panel, StatusBadge, EmptyState, ErrorState } from "@/components/ui";
import { Send, Check } from "lucide-react";
import { useState } from "react";

interface WebhookItem {
  id: number;
  event_id: string;
  status: string;
  rejection_reason: string | null;
  received_at: string | null;
  raw_payload: Record<string, unknown>;
}

export default function NotificationsPage() {
  const { data, error } = usePolling<WebhookItem[]>("/api/webhooks", 3000);
  const [sharing, setSharing] = useState<Record<string, boolean>>({});
  const [shared, setShared] = useState<Record<string, boolean>>({});

  const shareToTelegram = async (eventId: string) => {
    setSharing((prev) => ({ ...prev, [eventId]: true }));
    try {
      const res = await fetch(`/api/webhooks/${eventId}/share`, {
        method: "POST",
      });
      if (res.ok) {
        setShared((prev) => ({ ...prev, [eventId]: true }));
        setTimeout(() => {
          setShared((prev) => ({ ...prev, [eventId]: false }));
        }, 3000);
      }
    } catch (e) {
      console.error(e);
    } finally {
      setSharing((prev) => ({ ...prev, [eventId]: false }));
    }
  };

  return (
    <div className="space-y-4">
      <Panel title="Notifications (Received Webhooks)">
        {error && <ErrorState message={error} />}
        
        {(!data || data.length === 0) && !error ? (
          <EmptyState message="No webhooks received yet." />
        ) : (
          <div className="space-y-2">
            {(data || []).map((w) => {
              const p = (w.raw_payload ?? {}) as { symbol?: string; action?: string; strategy_id?: string };
              const isSharing = sharing[w.event_id];
              const isShared = shared[w.event_id];
              
              return (
                <div key={w.event_id} className="flex items-start justify-between border border-border/50 bg-panel2/30 p-3 rounded-md">
                  <div className="flex-1 min-w-0 pr-4">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="font-semibold text-[13px]">{p.strategy_id || "Unknown Strategy"}</span>
                      <span className="font-mono text-[12px] text-info">{p.symbol || "Unknown Symbol"}</span>
                      <span className={`font-semibold text-[12px] ${p.action === "BUY" || p.action === "LONG" ? "text-profit" : p.action === "SELL" || p.action === "SHORT" ? "text-loss" : ""}`}>{p.action || "Unknown Action"}</span>
                      <StatusBadge status={w.status} />
                    </div>
                    <div className="text-[11px] text-muted font-mono truncate mb-1">
                      Event ID: {w.event_id} • Received: {w.received_at ? new Date(w.received_at).toLocaleString() : "Unknown"}
                    </div>
                    {w.rejection_reason && (
                      <div className="text-[11px] text-warn">Reason: {w.rejection_reason}</div>
                    )}
                    <details className="mt-2 text-[10px] text-muted">
                      <summary className="cursor-pointer hover:text-txt">View Raw Payload</summary>
                      <pre className="mt-1 p-2 bg-panel rounded overflow-x-auto">
                        {JSON.stringify(w.raw_payload, null, 2)}
                      </pre>
                    </details>
                  </div>
                  <div className="shrink-0 flex items-center justify-center">
                    <button
                      onClick={() => shareToTelegram(w.event_id)}
                      disabled={isSharing || isShared}
                      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] font-medium transition-colors ${
                        isShared 
                          ? "bg-profit/10 text-profit border border-profit/20" 
                          : "bg-panel hover:bg-panel2 border border-border/50 text-txt"
                      } disabled:opacity-50 disabled:cursor-not-allowed`}
                    >
                      {isShared ? (
                        <>
                          <Check size={12} /> Shared
                        </>
                      ) : (
                        <>
                          <Send size={12} /> {isSharing ? "Sharing..." : "Share to Telegram"}
                        </>
                      )}
                    </button>
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
