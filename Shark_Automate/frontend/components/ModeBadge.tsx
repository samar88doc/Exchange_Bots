"use client";

import { usePolling } from "@/lib/api";
import type { StatusResponse } from "@/lib/types";

export function ModeBadge() {
  const { data } = usePolling<StatusResponse>("/api/status", 4000);
  const mode = data?.mode ?? "PAPER";
  const halted = data?.halted || data?.kill_switch;
  return (
    <div className="flex items-center gap-2">
      {mode === "LIVE" ? (
        <span className="badge badge-red animate-pulse">● LIVE MODE</span>
      ) : (
        <span className="badge badge-blue">● PAPER MODE</span>
      )}
      {halted && <span className="badge badge-amber">HALTED</span>}
    </div>
  );
}
