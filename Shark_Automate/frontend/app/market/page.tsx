"use client";

import { useState, useMemo, useRef, useEffect, useCallback } from "react";
import {
  TrendingUp,
  TrendingDown,
  Search,
  RefreshCw,
  ChevronUp,
  ChevronDown,
  Minus,
  Wifi,
  WifiOff,
  BarChart2,
} from "lucide-react";

interface TickerRow {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  last_price: number;
  open_price: number;
  high: number;
  low: number;
  volume: number;
  change_pct: number;
  change_abs: number;
  day_open: number;
  day_high: number;
  day_low: number;
  day_change_pct: number;
  status: string;
  webhook_status?: string;
}

type SortKey = keyof TickerRow | "hl_range" | "day_hl_range";
type SortDir = "asc" | "desc";

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtPrice(n: number): string {
  if (!n) return "—";
  const abs = Math.abs(n);
  const digits = abs >= 10000 ? 1 : abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 6 : 8;
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtVol(n: number): string {
  if (!n) return "—";
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + "B";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return n.toFixed(2);
}

function changeTone(pct: number) {
  if (pct > 0) return "text-profit";
  if (pct < 0) return "text-loss";
  return "text-muted";
}

function changeBg(pct: number) {
  if (pct > 0) return "bg-profit/10 border-profit/20";
  if (pct < 0) return "bg-loss/10 border-loss/20";
  return "bg-panel2/50 border-border/30";
}

// ── Sparkline (mini bar-chart for high/low range) ─────────────────────────────

function RangeBar({ low, high, last }: { low: number; high: number; last: number }) {
  if (!high || !low || high === low) return <div className="h-1 w-full bg-border/30 rounded" />;
  const pct = ((last - low) / (high - low)) * 100;
  const tone = pct > 50 ? "bg-profit" : "bg-loss";
  return (
    <div className="relative h-1 w-full bg-border/30 rounded overflow-hidden">
      <div className={`absolute left-0 top-0 h-full rounded ${tone}`} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
    </div>
  );
}

// ── Flash on price change ─────────────────────────────────────────────────────

function PriceCell({ price, pct }: { price: number; pct: number }) {
  const [flash, setFlash] = useState("");
  const prev = useRef(price);

  useEffect(() => {
    if (price !== prev.current) {
      setFlash(price > prev.current ? "flash-up" : "flash-down");
      prev.current = price;
      const t = setTimeout(() => setFlash(""), 600);
      return () => clearTimeout(t);
    }
  }, [price]);

  return (
    <span className={`font-mono font-semibold text-[13px] transition-colors ${changeTone(pct)} ${flash}`}>
      {fmtPrice(price)}
    </span>
  );
}

// ── Main Component ────────────────────────────────────────────────────────────

const LIMIT_OPTIONS = [50, 100, 200, 344];

export default function MarketPage() {
  const [data, setData] = useState<TickerRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("change_pct");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [limit, setLimit] = useState(344);
  const [filterDir, setFilterDir] = useState<"all" | "up" | "down">("all");
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  
  const [trendHistory, setTrendHistory] = useState<{ time: string; from: string; to: string }[]>([]);
  const prevTrend = useRef<string>("Waiting...");

  // Load persisted sentiment history from DB on mount (with retry if backend is starting up)
  useEffect(() => {
    let mounted = true;
    const loadHistory = async () => {
      while (mounted) {
        try {
          const r = await fetch("/api/market/sentiment-history?limit=50", { cache: "no-store" });
          if (r.ok) {
            const rows = await r.json();
            if (mounted && Array.isArray(rows)) {
              setTrendHistory(rows);
              // Seed prevTrend with the most recent sentiment so we don't double-record on first load
              // Only seed if it's still 'Waiting...' to avoid overwriting a live transition
              if (rows.length > 0 && prevTrend.current === "Waiting...") {
                prevTrend.current = rows[0].to;
              }
              break; // Success, exit retry loop
            }
          }
        } catch (e) {
          // Ignore network errors, will retry
        }
        if (mounted) await new Promise((res) => setTimeout(res, 3000));
      }
    };
    loadHistory();
    return () => { mounted = false; };
  }, []);

  const fetchData = useCallback(async (showSpinner = false) => {
    if (showSpinner) setRefreshing(true);
    try {
      const res = await fetch(`/api/market/live-feed?limit=${limit}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: TickerRow[] = await res.json();
      setData(json);
      setLastUpdated(new Date());
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "connection error");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [limit]);

  // Initial + interval
  useEffect(() => {
    setLoading(true);
    fetchData();
    intervalRef.current = setInterval(() => fetchData(), 5000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [fetchData]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir("desc"); }
  };

  const filtered = useMemo(() => {
    let rows = data.filter((r) => r.status === "ok" && r.last_price > 0);
    if (filterDir === "up") rows = rows.filter((r) => r.change_pct > 0);
    if (filterDir === "down") rows = rows.filter((r) => r.change_pct < 0);
    if (search.trim()) {
      const q = search.trim().toUpperCase();
      rows = rows.filter((r) => r.symbol.includes(q) || r.base_asset.includes(q));
    }
    rows.sort((a, b) => {
      let av: number | string;
      let bv: number | string;

      if (sortKey === "hl_range") {
        av = a.high === a.low || !a.high || !a.low ? 50 : ((a.last_price - a.low) / (a.high - a.low)) * 100;
        bv = b.high === b.low || !b.high || !b.low ? 50 : ((b.last_price - b.low) / (b.high - b.low)) * 100;
      } else if (sortKey === "day_hl_range") {
        av = a.day_high === a.day_low || !a.day_high || !a.day_low ? 50 : ((a.last_price - a.day_low) / (a.day_high - a.day_low)) * 100;
        bv = b.day_high === b.day_low || !b.day_high || !b.day_low ? 50 : ((b.last_price - b.day_low) / (b.day_high - b.day_low)) * 100;
      } else {
        av = a[sortKey as keyof TickerRow] as number | string;
        bv = b[sortKey as keyof TickerRow] as number | string;
      }

      if (typeof av === "number" && typeof bv === "number") {
        return sortDir === "asc" ? av - bv : bv - av;
      }
      return sortDir === "asc"
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });
    return rows;
  }, [data, search, sortKey, sortDir, filterDir]);

  const totalValid = useMemo(() => data.filter(r => r.status === "ok").length, [data]);
  const gainers = useMemo(() => data.filter((r) => r.change_pct > 0).length, [data]);
  const losers = useMemo(() => data.filter((r) => r.change_pct < 0).length, [data]);
  const totalVol = useMemo(() => data.reduce((s, r) => s + r.volume, 0), [data]);

  const sentiment = useMemo(() => {
    if (totalValid === 0) return { label: "Waiting...", tone: "text-muted", bg: "bg-panel2" };
    const gPct = gainers / totalValid;
    const lPct = losers / totalValid;
    if (gPct > 0.55) return { label: "Bullish", tone: "text-profit", bg: "bg-profit/10 border-profit/20" };
    if (lPct > 0.55) return { label: "Bearish", tone: "text-loss", bg: "bg-loss/10 border-loss/20" };
    return { label: "Neutral", tone: "text-info", bg: "bg-info/10 border-info/20" };
  }, [totalValid, gainers, losers]);

  useEffect(() => {
    if (sentiment.label !== "Waiting..." && sentiment.label !== prevTrend.current) {
      if (prevTrend.current !== "Waiting...") {
        const from = prevTrend.current;
        const to = sentiment.label;
        // Persist to DB
        fetch("/api/market/sentiment-history", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ from, to }),
        })
          .then((r) => r.ok ? r.json() : null)
          .then((saved) => {
            if (saved) {
              setTrendHistory((prev) => [{ time: saved.time, from, to }, ...prev].slice(0, 50));
            }
          })
          .catch(() => {
            // fallback: add in-memory only
            setTrendHistory((prev) => [{ time: new Date().toISOString(), from, to }, ...prev].slice(0, 50));
          });
      }
      prevTrend.current = sentiment.label;
    }
  }, [sentiment.label]);

  const SortHeader = ({ col, label }: { col: SortKey; label: string }) => (
    <th
      className="th px-3 py-2 cursor-pointer hover:text-txt select-none whitespace-nowrap"
      onClick={() => handleSort(col)}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {sortKey === col ? (
          sortDir === "asc" ? <ChevronUp size={11} /> : <ChevronDown size={11} />
        ) : (
          <span className="opacity-0 group-hover:opacity-40"><ChevronDown size={11} /></span>
        )}
      </span>
    </th>
  );

  return (
    <div className="space-y-4">
      {/* ── Header ── */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-[15px] font-bold flex items-center gap-2">
            <BarChart2 size={18} className="text-info" />
            SharkExchange Live Feed
          </h1>
          <p className="text-[11px] text-muted mt-0.5">
            Real-time 24h ticker data · auto-refresh every 5 s
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <select
            className="input text-[11.5px] py-1"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
          >
            {LIMIT_OPTIONS.map((l) => (
              <option key={l} value={l}>{l} symbols</option>
            ))}
          </select>
          <button
            className="btn"
            onClick={() => fetchData(true)}
            disabled={refreshing}
          >
            <RefreshCw size={12} className={refreshing ? "animate-spin" : ""} />
            Refresh
          </button>
          <div className={`flex items-center gap-1.5 text-[11px] ${error ? "text-loss" : "text-profit"}`}>
            {error ? <WifiOff size={12} /> : <Wifi size={12} />}
            {error ? "Disconnected" : lastUpdated ? lastUpdated.toLocaleTimeString() : "—"}
          </div>
        </div>
      </div>

      {/* ── Summary Stats ── */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <div className={`panel px-3 py-2.5 border ${sentiment.bg}`}>
          <div className="text-[10px] text-muted uppercase tracking-wider mb-0.5">Sentiment</div>
          <div className={`text-[18px] font-bold ${sentiment.tone}`}>{sentiment.label}</div>
        </div>
        {[
          { label: "Total Symbols", value: totalValid.toString(), tone: "" },
          { label: "Gainers", value: gainers.toString(), tone: "text-profit" },
          { label: "Losers", value: losers.toString(), tone: "text-loss" },
          { label: "Total Volume", value: fmtVol(totalVol), tone: "text-info" },
        ].map(({ label, value, tone }) => (
          <div key={label} className="panel px-3 py-2.5">
            <div className="text-[10px] text-muted uppercase tracking-wider mb-0.5">{label}</div>
            <div className={`text-[18px] font-bold font-mono ${tone}`}>{value}</div>
          </div>
        ))}
      </div>

      {/* ── Filters ── */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 min-w-[180px] max-w-[280px]">
          <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
          <input
            className="input w-full pl-7 text-[12px]"
            placeholder="Search symbol…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="flex rounded border border-border overflow-hidden text-[11.5px]">
          {(["all", "up", "down"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilterDir(f)}
              className={`px-3 py-1.5 transition-colors ${
                filterDir === f
                  ? f === "up" ? "bg-profit/15 text-profit" : f === "down" ? "bg-loss/15 text-loss" : "bg-info/15 text-info"
                  : "text-muted hover:text-txt hover:bg-panel2"
              }`}
            >
              {f === "all" ? "All" : f === "up" ? "▲ Gainers" : "▼ Losers"}
            </button>
          ))}
        </div>
        <span className="text-[11px] text-muted ml-auto">{filtered.length} showing</span>
      </div>

      {/* ── Trend History ── */}
      {trendHistory.length > 0 && (
        <div className="panel p-3 max-h-[150px] overflow-y-auto text-[11.5px]">
          <div className="text-muted font-bold mb-2 uppercase text-[10px] tracking-wider">Sentiment History (Persisted)</div>
          <div className="space-y-1.5">
            {trendHistory.map((h, i) => (
              <div key={i} className="flex items-center gap-3">
                <span className="text-muted font-mono">{new Date(h.time).toLocaleString()}</span>
                <span className="text-muted line-through">{h.from}</span>
                <span className="text-txt">→</span>
                <span className={h.to === "Bullish" ? "text-profit font-bold" : h.to === "Bearish" ? "text-loss font-bold" : "text-info font-bold"}>{h.to}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Table ── */}
      {loading ? (
        <div className="panel p-8 text-center text-muted text-[13px]">
          <RefreshCw size={20} className="animate-spin mx-auto mb-3 text-info" />
          Fetching live prices for {limit} symbols…
        </div>
      ) : error && data.length === 0 ? (
        <div className="panel p-6 text-center text-loss text-[13px]">{error}</div>
      ) : (
        <div className="panel overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-border bg-panel2/60">
                <th className="th px-3 py-2 w-8">#</th>
                <SortHeader col="symbol" label="Symbol" />
                <SortHeader col="last_price" label="Last Price" />
                <SortHeader col="change_pct" label="24h %" />
                <SortHeader col="high" label="24h High" />
                <SortHeader col="low" label="24h Low" />
                <SortHeader col="hl_range" label="H/L Range" />
                <SortHeader col="day_change_pct" label="Day %" />
                <SortHeader col="day_hl_range" label="Day H/L" />
                <SortHeader col="volume" label="24h Volume" />
                <th className="th px-3 py-2 w-24">Webhook Status</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row, i) => (
                <tr
                  key={row.symbol}
                  className="border-b border-border/40 row-hover transition-colors"
                >
                  <td className="td text-muted text-[11px] w-8">{i + 1}</td>

                  {/* Symbol */}
                  <td className="td px-3 py-2">
                    <div className="flex items-center gap-2">
                      <div className={`w-6 h-6 rounded flex items-center justify-center text-[9px] font-bold shrink-0 ${
                        row.change_pct > 0
                          ? "bg-profit/15 text-profit"
                          : row.change_pct < 0
                          ? "bg-loss/15 text-loss"
                          : "bg-panel2 text-muted"
                      }`}>
                        {row.base_asset?.slice(0, 2) || row.symbol.slice(0, 2)}
                      </div>
                      <div>
                        <div className="font-semibold text-[12.5px] text-txt leading-tight">{row.base_asset || row.symbol}</div>
                        <div className="text-[10px] text-muted leading-tight">{row.symbol}</div>
                      </div>
                    </div>
                  </td>

                  {/* Price */}
                  <td className="td px-3 py-2">
                    <PriceCell price={row.last_price} pct={row.change_pct} />
                  </td>

                  {/* 24h % */}
                  <td className="td px-3 py-2">
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[11.5px] font-semibold ${changeBg(row.change_pct)} ${changeTone(row.change_pct)}`}>
                      {row.change_pct > 0 ? (
                        <TrendingUp size={10} />
                      ) : row.change_pct < 0 ? (
                        <TrendingDown size={10} />
                      ) : (
                        <Minus size={10} />
                      )}
                      {row.change_pct >= 0 ? "+" : ""}{row.change_pct.toFixed(3)}%
                    </span>
                  </td>

                  {/* High */}
                  <td className="td px-3 py-2 font-mono text-[12px] text-profit/80">{fmtPrice(row.high)}</td>

                  {/* Low */}
                  <td className="td px-3 py-2 font-mono text-[12px] text-loss/80">{fmtPrice(row.low)}</td>

                  {/* H/L Range bar */}
                  <td className="td px-3 py-2 min-w-[80px]">
                    <RangeBar low={row.low} high={row.high} last={row.last_price} />
                    <div className="flex justify-between items-center text-[9px] text-muted mt-0.5">
                      <span>L</span>
                      <span>
                        {row.high && row.low && row.high !== row.low 
                          ? `${(((row.last_price - row.low) / (row.high - row.low)) * 100).toFixed(0)}%` 
                          : '-'}
                      </span>
                      <span>H</span>
                    </div>
                  </td>

                  {/* Day Change % (5:30 AM IST daily candle) */}
                  <td className="td px-3 py-2">
                    {row.day_change_pct !== 0 || row.day_open > 0 ? (
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[11.5px] font-semibold ${changeBg(row.day_change_pct)} ${changeTone(row.day_change_pct)}`}>
                        {row.day_change_pct > 0 ? (
                          <TrendingUp size={10} />
                        ) : row.day_change_pct < 0 ? (
                          <TrendingDown size={10} />
                        ) : (
                          <Minus size={10} />
                        )}
                        {row.day_change_pct >= 0 ? "+" : ""}{row.day_change_pct.toFixed(3)}%
                      </span>
                    ) : (
                      <span className="text-muted text-[11px]">—</span>
                    )}
                  </td>

                  {/* Day H/L Range bar */}
                  <td className="td px-3 py-2 min-w-[90px]">
                    {row.day_high > 0 && row.day_low > 0 ? (
                      <>
                        <RangeBar low={row.day_low} high={row.day_high} last={row.last_price} />
                        <div className="flex justify-between items-center text-[9px] text-muted mt-0.5">
                          <span className="text-loss/70">{fmtPrice(row.day_low)}</span>
                          <span>
                            {row.day_high !== row.day_low
                              ? `${(((row.last_price - row.day_low) / (row.day_high - row.day_low)) * 100).toFixed(0)}%`
                              : "-"}
                          </span>
                          <span className="text-profit/70">{fmtPrice(row.day_high)}</span>
                        </div>
                      </>
                    ) : (
                      <span className="text-muted text-[11px]">—</span>
                    )}
                  </td>

                  {/* Volume */}
                  <td className="td px-3 py-2 text-[12px] font-mono text-muted">{fmtVol(row.volume)}</td>

                  {/* Webhook Status */}
                  <td className="td px-3 py-2 text-[11px] font-bold">
                    {row.webhook_status && (
                      <span className={`px-2 py-1 rounded border ${row.webhook_status === 'BUY' ? 'bg-profit/15 text-profit border-profit/20' : row.webhook_status === 'SELL' ? 'bg-loss/15 text-loss border-loss/20' : 'bg-info/15 text-info border-info/20'}`}>
                        {row.webhook_status}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={11} className="text-center py-10 text-muted text-[13px]">
                    No symbols match your filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
