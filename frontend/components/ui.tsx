import Link from "next/link";
import type { LucideIcon } from "lucide-react";
import { AlertTriangle, CheckCircle2, ChevronRight, Clock, Loader2, PauseCircle, XCircle } from "lucide-react";

// ---------------------------------------------------------------------------
// Page header with optional breadcrumbs and right-aligned actions
// ---------------------------------------------------------------------------

export interface Crumb {
  label: string;
  href?: string;
}

interface PageHeaderProps {
  title: React.ReactNode;
  description?: React.ReactNode;
  breadcrumbs?: Crumb[];
  actions?: React.ReactNode;
  meta?: React.ReactNode;
}

export function PageHeader({ title, description, breadcrumbs, actions, meta }: PageHeaderProps) {
  return (
    <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
      <div className="min-w-0 space-y-1.5">
        {breadcrumbs && breadcrumbs.length > 0 && (
          <nav aria-label="Breadcrumb" className="flex items-center gap-1 text-2xs font-medium text-slate-500">
            {breadcrumbs.map((c, i) => (
              <span key={`${c.label}-${i}`} className="flex items-center gap-1 min-w-0">
                {i > 0 && <ChevronRight className="w-3 h-3 text-slate-300 shrink-0" />}
                {c.href ? (
                  <Link href={c.href} className="hover:text-slate-900 transition-colors truncate">
                    {c.label}
                  </Link>
                ) : (
                  <span className="text-slate-700 truncate">{c.label}</span>
                )}
              </span>
            ))}
          </nav>
        )}
        <h1 className="text-xl sm:text-2xl font-bold tracking-tight text-slate-900 flex items-center gap-2.5 flex-wrap">
          {title}
        </h1>
        {description && <p className="text-sm text-slate-500 max-w-2xl">{description}</p>}
        {meta && <div className="pt-1">{meta}</div>}
      </div>
      {actions && <div className="flex items-center gap-2.5 flex-wrap shrink-0">{actions}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Job status badge - one source of truth for status colors/icons
// ---------------------------------------------------------------------------

const STATUS_CONFIG: Record<string, { label: string; cls: string; icon: LucideIcon; spin?: boolean }> = {
  COMPLETE: { label: "Complete", cls: "bg-emerald-50 text-emerald-700 border-emerald-200", icon: CheckCircle2 },
  RUNNING: { label: "Running", cls: "bg-blue-50 text-blue-700 border-blue-200", icon: Loader2, spin: true },
  PENDING: { label: "Pending", cls: "bg-slate-100 text-slate-600 border-slate-200", icon: Clock },
  FAILED: { label: "Failed", cls: "bg-rose-50 text-rose-700 border-rose-200", icon: XCircle },
  AWAITING_APPROVAL: {
    label: "Awaiting approval",
    cls: "bg-amber-50 text-amber-800 border-amber-200",
    icon: PauseCircle,
  },
  REJECTED: { label: "Rejected", cls: "bg-slate-100 text-slate-700 border-slate-300", icon: AlertTriangle },
};

export function StatusBadge({ status, size = "sm" }: { status: string; size?: "sm" | "md" }) {
  const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.PENDING;
  const Icon = cfg.icon;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border font-semibold whitespace-nowrap ${cfg.cls} ${
        size === "md" ? "px-3 py-1 text-xs" : "px-2.5 py-0.5 text-2xs"
      }`}
    >
      <Icon className={`${size === "md" ? "w-3.5 h-3.5" : "w-3 h-3"} ${cfg.spin ? "animate-spin" : ""}`} />
      {cfg.label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// KPI / stat tile
// ---------------------------------------------------------------------------

const TONES = {
  slate: { icon: "bg-slate-100 text-slate-600", value: "text-slate-900" },
  brand: { icon: "bg-brand-50 text-brand-600", value: "text-slate-900" },
  emerald: { icon: "bg-emerald-50 text-emerald-600", value: "text-emerald-700" },
  amber: { icon: "bg-amber-50 text-amber-600", value: "text-amber-700" },
  rose: { icon: "bg-rose-50 text-rose-600", value: "text-rose-700" },
  blue: { icon: "bg-blue-50 text-blue-600", value: "text-blue-700" },
  indigo: { icon: "bg-indigo-50 text-indigo-600", value: "text-indigo-700" },
  purple: { icon: "bg-purple-50 text-purple-600", value: "text-purple-700" },
} as const;

export type Tone = keyof typeof TONES;

interface StatCardProps {
  label: string;
  value: React.ReactNode;
  icon?: LucideIcon;
  tone?: Tone;
  hint?: React.ReactNode;
  compact?: boolean;
}

export function StatCard({ label, value, icon: Icon, tone = "slate", hint, compact = false }: StatCardProps) {
  const t = TONES[tone];
  return (
    <div className={`card ${compact ? "p-3.5" : "p-4 sm:p-5"} flex items-start gap-3.5`}>
      {Icon && (
        <div className={`rounded-xl shrink-0 ${compact ? "p-2" : "p-2.5"} ${t.icon}`}>
          <Icon className={compact ? "w-4 h-4" : "w-5 h-5"} />
        </div>
      )}
      <div className="min-w-0">
        <div className="text-2xs font-semibold uppercase tracking-wider text-slate-500 truncate">{label}</div>
        <div className={`${compact ? "text-lg" : "text-2xl"} font-bold tabular-nums leading-tight mt-0.5 ${t.value}`}>
          {value}
        </div>
        {hint && <div className="text-2xs text-slate-500 mt-0.5 truncate">{hint}</div>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section heading used inside pages
// ---------------------------------------------------------------------------

export function SectionHeading({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-end justify-between gap-3">
      <div>
        <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
          {Icon && <Icon className="w-4 h-4 text-brand-600" />}
          {title}
        </h2>
        {description && <p className="text-xs text-slate-500 mt-0.5">{description}</p>}
      </div>
      {action}
    </div>
  );
}
