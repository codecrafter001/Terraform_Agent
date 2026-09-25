"use client";

import { useEffect, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { LucideIcon } from "lucide-react";
import { LayoutDashboard, Menu, PlusCircle, ShieldCheck, X } from "lucide-react";
import { checkHealth } from "@/lib/api";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  // Extra path prefixes that should also highlight this item (e.g. a job's
  // progress/results pages live under the dashboard's job history).
  matches?: string[];
}

const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard, matches: ["/results", "/scan/"] },
  { href: "/scan", label: "New Scan", icon: PlusCircle },
];

function isActive(pathname: string, item: NavItem): boolean {
  if (item.href === "/") {
    return pathname === "/" || (item.matches ?? []).some((m) => pathname.startsWith(m));
  }
  return pathname === item.href;
}

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5 px-2 py-1 rounded-xl hover:opacity-90 transition-opacity">
      <Image
        src="/aikart-logo.jpeg"
        alt="AIKart"
        width={96}
        height={28}
        className="h-7 w-auto object-contain"
        priority
      />
      <span className="w-px h-5 bg-slate-200" />
      <span className="text-sm font-bold text-ink whitespace-nowrap">
        Terra<span className="text-brand-600">Agent</span>
      </span>
    </Link>
  );
}

function HealthIndicator() {
  const [healthy, setHealthy] = useState<boolean | null>(null);

  useEffect(() => {
    let mounted = true;
    const check = async () => {
      const ok = await checkHealth();
      if (mounted) setHealthy(ok);
    };
    check();
    const interval = setInterval(check, 30000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  return (
    <div className="flex items-center gap-2 text-2xs font-medium text-slate-600">
      <span
        className={`w-2 h-2 rounded-full ${
          healthy === null ? "bg-slate-300" : healthy ? "bg-emerald-500" : "bg-rose-500"
        }`}
      />
      {healthy === null ? "Checking API..." : healthy ? "API healthy" : "API unreachable"}
    </div>
  );
}

function NavLinks({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  return (
    <nav className="space-y-1">
      {NAV_ITEMS.map((item) => {
        const active = isActive(pathname, item);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={`flex items-center gap-3 px-3 py-2 rounded-xl text-sm font-medium transition-colors ${
              active
                ? "bg-brand-50 text-brand-700"
                : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
            }`}
          >
            <Icon className={`w-4 h-4 ${active ? "text-brand-600" : "text-slate-400"}`} />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function SidebarFooter() {
  return (
    <div className="space-y-3">
      <div className="p-3 rounded-xl bg-brand-50/70 border border-brand-100 space-y-1.5">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-brand-900">
          <ShieldCheck className="w-4 h-4 text-brand-600" />
          Read-only guardrails
        </div>
        <p className="text-2xs text-brand-900/70 leading-relaxed">
          Never runs <code className="font-mono">apply</code>, <code className="font-mono">destroy</code> or{" "}
          <code className="font-mono">import</code>. AWS access is Describe/Get/List only.
        </p>
      </div>
      <div className="flex items-center justify-between px-1">
        <HealthIndicator />
        <span className="text-3xs font-mono text-slate-400">v1.0</span>
      </div>
    </div>
  );
}

export function Sidebar() {
  const pathname = usePathname() ?? "/";
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <>
      {/* Desktop sidebar */}
      <aside className="hidden lg:flex lg:flex-col lg:fixed lg:inset-y-0 lg:left-0 lg:w-64 border-r border-slate-200 bg-white z-40">
        <div className="h-16 flex items-center px-4 border-b border-slate-100">
          <Brand />
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-5 space-y-6">
          <div className="space-y-2">
            <div className="px-3 text-3xs font-bold uppercase tracking-wider text-slate-400">Workspace</div>
            <NavLinks pathname={pathname} />
          </div>
        </div>
        <div className="p-3 border-t border-slate-100">
          <SidebarFooter />
        </div>
      </aside>

      {/* Mobile top bar */}
      <header className="lg:hidden sticky top-0 z-40 h-14 flex items-center justify-between px-4 border-b border-slate-200 bg-white/90 backdrop-blur-md">
        <Brand />
        <button
          onClick={() => setMobileOpen(true)}
          className="p-2 rounded-lg text-slate-600 hover:bg-slate-100"
          aria-label="Open navigation"
        >
          <Menu className="w-5 h-5" />
        </button>
      </header>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="lg:hidden fixed inset-0 z-50">
          <div className="absolute inset-0 bg-slate-900/30 backdrop-blur-xs" onClick={() => setMobileOpen(false)} />
          <div className="absolute inset-y-0 left-0 w-72 max-w-[85vw] bg-white shadow-xl flex flex-col animate-fade-in">
            <div className="h-14 flex items-center justify-between px-4 border-b border-slate-100">
              <Brand />
              <button
                onClick={() => setMobileOpen(false)}
                className="p-2 rounded-lg text-slate-500 hover:bg-slate-100"
                aria-label="Close navigation"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="flex-1 px-3 py-5">
              <NavLinks pathname={pathname} onNavigate={() => setMobileOpen(false)} />
            </div>
            <div className="p-3 border-t border-slate-100">
              <SidebarFooter />
            </div>
          </div>
        </div>
      )}
    </>
  );
}
