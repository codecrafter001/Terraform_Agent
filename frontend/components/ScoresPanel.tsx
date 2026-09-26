import { ShieldCheck, ArrowLeftRight } from "lucide-react";
import { MigrationSafety, SecurityPosture } from "@/lib/types";

const SAFETY_TONE: Record<MigrationSafety["status"], string> = {
  SAFE: "text-emerald-700",
  CHANGES: "text-amber-700",
  DESTRUCTIVE: "text-rose-700",
  UNVERIFIED: "text-slate-500",
};

const POSTURE_TONE: Record<SecurityPosture["rating"], string> = {
  GOOD: "text-emerald-700",
  FAIR: "text-amber-700",
  POOR: "text-rose-700",
  UNKNOWN: "text-slate-500",
};

const BASIS: Record<MigrationSafety["basis"], string> = {
  plan: "terraform plan against live AWS",
  drift: "live attributes vs generated code (no plan)",
  none: "no evidence",
};

/** Migration Safety and Security Posture side by side - two questions, never one number. */
export default function ScoresPanel({
  safety,
  posture,
}: {
  safety?: MigrationSafety | null;
  posture?: SecurityPosture | null;
}) {
  if (!safety && !posture) return null;
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div className="card p-5 space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <ArrowLeftRight className="w-4 h-4 text-brand-600" />
              Migration Safety
            </h2>
            <p className="text-2xs text-slate-500 mt-0.5">Will adopting this change anything in AWS?</p>
          </div>
          {safety && (
            <div className={`text-2xl font-bold tabular-nums ${SAFETY_TONE[safety.status]}`}>
              {safety.score === null ? "—" : `${safety.score}%`}
            </div>
          )}
        </div>
        {safety ? (
          <div className="text-xs text-slate-700 space-y-1.5">
            <div>
              <span className={`font-bold ${SAFETY_TONE[safety.status]}`}>{safety.status}</span>
              <span className="text-slate-500"> · basis: {BASIS[safety.basis]}</span>
            </div>
            <div className="text-slate-600">{safety.reason}</div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-2xs text-slate-600">
              <span>{safety.resources_managed} managed</span>
              {safety.no_op !== undefined && <span>{safety.no_op} unchanged</span>}
              <span className={safety.destroy_or_replace ? "text-rose-700 font-bold" : ""}>
                {safety.destroy_or_replace} destroy/replace
              </span>
              {safety.config_mismatches != null && <span>{safety.config_mismatches} config mismatch(es)</span>}
            </div>
            {safety.changing_resources.length > 0 && (
              <div className="text-2xs font-mono text-amber-800 bg-amber-50 border border-amber-100 rounded-lg p-2 break-all">
                Would change: {safety.changing_resources.join(", ")}
              </div>
            )}
          </div>
        ) : (
          <div className="text-xs text-slate-500">Not measured.</div>
        )}
      </div>

      <div className="card p-5 space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <ShieldCheck className="w-4 h-4 text-brand-600" />
              Security Posture
            </h2>
            <p className="text-2xs text-slate-500 mt-0.5">
              What&apos;s wrong with the current setup? Reported, never auto-fixed in the adoption.
            </p>
          </div>
          {posture && (
            <div className={`text-2xl font-bold tabular-nums ${POSTURE_TONE[posture.rating]}`}>
              {posture.score === null ? "—" : posture.score}
              {posture.score !== null && <span className="text-xs text-slate-400 font-medium">/100</span>}
            </div>
          )}
        </div>
        {posture ? (
          <div className="text-xs text-slate-700 space-y-1.5">
            <div>
              <span className={`font-bold ${POSTURE_TONE[posture.rating]}`}>{posture.rating}</span>
              <span className="text-slate-500"> · {posture.reason}</span>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-2xs">
              <span className="text-rose-700">{posture.counts.critical} critical</span>
              <span className="text-orange-700">{posture.counts.high} high</span>
              <span className="text-amber-700">{posture.counts.medium} medium</span>
              <span className="text-slate-600">{posture.counts.low} low</span>
            </div>
            <div className="text-2xs text-slate-500">
              Scanners: {posture.scanners_run.join(", ") || "none"}
              {posture.scanners_missing.length > 0 && ` · not installed: ${posture.scanners_missing.join(", ")}`}
              {Object.keys(posture.scanners_failed).length > 0 &&
                ` · failed: ${Object.keys(posture.scanners_failed).join(", ")}`}
            </div>
          </div>
        ) : (
          <div className="text-xs text-slate-500">Not measured.</div>
        )}
      </div>
    </div>
  );
}
