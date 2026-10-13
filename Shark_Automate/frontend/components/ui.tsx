"use client";

import { ReactNode } from "react";

export function Panel({
  title,
  right,
  children,
  className = "",
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between px-3 py-2 border-b border-border">
          <h2 className="th">{title}</h2>
          {right}
        </header>
      )}
      <div className="p-3">{children}</div>
    </section>
  );
}

export function Stat({ label, value, tone = "" }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="px-3 py-2 bg-panel2 rounded border border-border">
      <div className="th">{label}</div>
      <div className={`text-lg font-semibold font-mono ${tone}`}>{value}</div>
    </div>
  );
}

export function SymTriple({ raw, canonical, shark }: { raw: string; canonical: string; shark: string }) {
  return (
    <div className="sym-triple text-muted">
      <div>
        <span className="text-[9.5px] uppercase">TV:</span> <span className="text-txt">{raw}</span>
      </div>
      <div>
        <span className="text-[9.5px] uppercase">CAN:</span> <span className="text-txt">{canonical}</span>
      </div>
      <div>
        <span className="text-[9.5px] uppercase">SHARK:</span> <span className="text-txt">{shark}</span>
      </div>
    </div>
  );
}

const statusTone: Record<string, string> = {
  EXECUTED: "badge-green",
  ACCEPTED: "badge-blue",
  RECEIVED: "badge-blue",
  VALIDATED: "badge-blue",
  CONNECTED: "badge-green",
  FILLED: "badge-green",
  CLOSED: "badge-neutral",
  WIN: "badge-green",
  OPEN: "badge-green",
  MONITORING: "badge-green",
  REJECTED: "badge-red",
  DUPLICATE: "badge-amber",
  MAX_POSITIONS: "badge-amber",
  SYMBOL_ALREADY_OPEN: "badge-amber",
  SHARK_SYMBOL_NOT_FOUND: "badge-red",
  INVALID: "badge-red",
  ERROR: "badge-red",
  UNKNOWN: "badge-red",
  DISCONNECTED: "badge-red",
  DEGRADED: "badge-amber",
  RESERVED: "badge-amber",
  ORDER_PENDING: "badge-amber",
  ORDER_SUBMITTED: "badge-amber",
  CLOSE_PENDING: "badge-amber",
  TP_TRIGGERED: "badge-blue",
  DRY_RUN: "badge-amber",
  DRY: "badge-amber",
  LOSS: "badge-red",
  PENDING: "badge-amber",
  NEW: "badge-blue",
  PARTIALLY_FILLED: "badge-amber",
  CANCELED: "badge-neutral",
  CANCELLED: "badge-neutral",
  FAILED: "badge-red",
  LOADING: "badge-neutral",
};

export function StatusBadge({ status }: { status: string }) {
  const tone = statusTone[status] ?? "badge-neutral";
  return <span className={`badge ${tone}`}>{status.replace(/_/g, " ")}</span>;
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="text-center text-muted text-sm py-10 border border-dashed border-border rounded">
      {message}
    </div>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div className="text-center text-loss text-sm py-6 border border-loss/40 bg-loss/5 rounded">
      {message}
    </div>
  );
}

export function pnlTone(v: string | number): string {
  const n = parseFloat(String(v));
  if (Number.isNaN(n) || n === 0) return "text-txt";
  return n > 0 ? "text-profit" : "text-loss";
}
