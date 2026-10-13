"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  ArrowLeftRight,
  BarChart2,
  Bell,
  History,
  LayoutDashboard,
  ListChecks,
  Settings2,
  ShieldAlert,
  PlayCircle,
} from "lucide-react";
import { ModeBadge } from "./ModeBadge";

const links = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/signals", label: "Signals", icon: ListChecks },
  { href: "/positions", label: "Positions", icon: ArrowLeftRight },
  { href: "/trades", label: "Trade History", icon: History },
  { href: "/risk", label: "Risk Center", icon: ShieldAlert },
  { href: "/webhooks", label: "Webhook Monitor", icon: Activity },
  { href: "/market", label: "Market Live Feed", icon: BarChart2 },
  { href: "/replay", label: "Replay", icon: PlayCircle },
  { href: "/audit", label: "Audit & Events", icon: Bell },
  { href: "/notifications", label: "Notifications", icon: Bell },
  { href: "/settings", label: "Settings / Go Live", icon: Settings2 },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <aside className="w-56 shrink-0 border-r border-border bg-panel/60 flex flex-col">
      <div className="px-4 py-4 border-b border-border">
        <div className="text-sm font-bold tracking-tight">TV → SHARK</div>
        <div className="text-[10.5px] text-muted uppercase tracking-widest">Trading Terminal</div>
        <div className="mt-2">
          <ModeBadge />
        </div>
      </div>
      <nav className="flex-1 py-2">
        {links.map(({ href, label, icon: Icon }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-2.5 px-4 py-2 text-[13px] border-l-2 transition-colors ${
                active
                  ? "border-info text-txt bg-panel2"
                  : "border-transparent text-muted hover:text-txt hover:bg-panel2/60"
              }`}
            >
              <Icon size={15} />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="px-4 py-3 border-t border-border text-[10.5px] text-muted">
        Personal trading control platform
        <br />
        Stop Loss: <span className="text-warn font-semibold">DISABLED</span>
      </div>
    </aside>
  );
}
