"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

export async function apiSend<T>(path: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.detail || `${path}: ${res.status}`);
  return data as T;
}

/**
 * Standalone SSE hook — use only when you explicitly need push events.
 * NOT used inside usePolling to avoid N connections per page.
 */
export function useLiveUpdates(onUpdate: () => void) {
  const saved = useRef(onUpdate);
  saved.current = onUpdate;

  useEffect(() => {
    let es: EventSource | null = null;
    try {
      es = new EventSource("/api/stream");
      es.onmessage = (ev) => {
        try { JSON.parse(ev.data); saved.current(); } catch { /* heartbeat */ }
      };
    } catch { /* SSE unavailable */ }
    return () => { es?.close(); };
  }, []);
}

/**
 * Dead-simple, StrictMode-safe polling hook.
 * - Fetches immediately on mount / path change (keeps old data visible while re-fetching)
 * - Polls on its own setInterval — NO SSE dependency
 * - Guards setState against unmounted component updates
 */
export function usePolling<T>(path: string, intervalMs = 4000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const result = await apiGet<T>(path);
      if (mountedRef.current) { setData(result); setError(""); }
    } catch (e) {
      if (mountedRef.current) setError(e instanceof Error ? e.message : "connection error");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    mountedRef.current = true;
    setLoading(true);    // show loading on path change
    refresh();           // immediate first fetch
    const id = setInterval(refresh, intervalMs);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [refresh, intervalMs]);

  return { data, error, loading, refresh };
}

export const fmtNum = (v: string | number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : parseFloat(v);
  if (Number.isNaN(n)) return String(v);
  return n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: digits });
};

/**
 * Smart price formatter — automatically picks enough decimal places
 * based on the magnitude so low-value coins show full precision:
 *   ≥ 1000  → 2 dp  (e.g. BTC  67432.15)
 *   ≥ 1     → 4 dp  (e.g. ETH  3421.4567)
 *   ≥ 0.01  → 6 dp  (e.g. XRP  0.524316)
 *   < 0.01  → 8 dp  (e.g. BAS  0.02435670)
 */
export const fmtPrice = (v: string | number | null | undefined): string => {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : parseFloat(v);
  if (Number.isNaN(n)) return String(v);
  const abs = Math.abs(n);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 6 : 8;
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

export const fmtPnl = (v: string | number | null | undefined): string => {
  const n = parseFloat(String(v ?? "0"));
  return `${n >= 0 ? "+" : ""}${fmtNum(n, 4)}`;
};

export const fmtTime = (iso: string | null | undefined, tz = "Asia/Kolkata"): string => {
  if (!iso) return "—";
  try {
    return new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").toLocaleString("en-GB", {
      timeZone: tz,
      hour12: false,
    });
  } catch {
    return iso;
  }
};
