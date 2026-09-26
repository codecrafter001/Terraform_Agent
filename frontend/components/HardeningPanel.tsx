import { Wrench, AlertTriangle, CheckCircle2, DollarSign } from "lucide-react";
import { CostResults, Hardening } from "@/lib/types";

function costLine(cost?: CostResults): string {
  if (!cost || cost.skipped) return "No cost change.";
  if (cost.tool_skipped || cost.monthly_delta == null) {
    return "Cost delta not estimated (Infracost unavailable) - this is not $0.";
  }
  const sign = cost.monthly_delta >= 0 ? "+" : "−";
  return `${sign}${Math.abs(cost.monthly_delta).toFixed(2)} ${cost.currency ?? "USD"}/month`;
}

/** The optional Hardening proposal - separate from the zero-change adoption code. */
export default function HardeningPanel({ hardening }: { hardening?: Hardening }) {
  const changes = hardening?.changes ?? [];
  const recommendations = hardening?.recommendations ?? [];
  const files = Object.keys(hardening?.files ?? {});
  if (!hardening || (changes.length === 0 && recommendations.length === 0)) {
    return (
      <div className="card p-5 text-xs text-slate-500">
        <span className="font-semibold text-slate-700">Hardening: </span>
        nothing to propose - no automatic fixes apply and no high or critical findings need manual follow-up.
      </div>
    );
  }

  return (
    <div className="card overflow-hidden">
      <div className="card-header">
        <div>
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <Wrench className="w-4 h-4 text-brand-600" />
            Hardening proposal (optional)
          </h2>
          <p className="text-2xs text-slate-500 mt-0.5">
            Security fixes for a separate PR on top of the adoption - never mixed into it. Each one changes live
            behavior when applied, so review before merging.
          </p>
        </div>
        <div className="text-right shrink-0 text-2xs text-slate-600 flex items-center gap-1">
          <DollarSign className="w-3.5 h-3.5 text-slate-400" />
          {costLine(hardening.cost)}
        </div>
      </div>

      <div className="p-5 space-y-3">
        {hardening.rejected_reason ? (
          <div className="p-3 rounded-xl border border-rose-200 bg-rose-50 text-xs text-rose-800 flex gap-2">
            <AlertTriangle className="w-4 h-4 shrink-0" />
            Not shipped as code: {hardening.rejected_reason}. The changes below are listed for manual follow-up.
          </div>
        ) : (
          files.length > 0 && (
            <div className="p-3 rounded-xl border border-emerald-200 bg-emerald-50 text-xs text-emerald-800 flex gap-2">
              <CheckCircle2 className="w-4 h-4 shrink-0" />
              Validated with terraform validate · {files.length} changed file(s): {files.join(", ")} (under
              hardening/ in the bundle)
            </div>
          )
        )}

        {changes.map((c, i) => (
          <div key={i} className="p-4 rounded-xl border border-slate-200 bg-white space-y-1.5">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-bold text-slate-900">{c.title}</span>
              <span
                className={`text-3xs font-bold uppercase px-2 py-0.5 rounded-full border ${
                  c.impact === "safe"
                    ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                    : "bg-amber-50 text-amber-700 border-amber-200"
                }`}
              >
                {c.impact.replace(/_/g, " ")}
              </span>
            </div>
            <p className="text-xs text-slate-700">{c.explanation}</p>
            <p className="text-2xs text-slate-500">Risk: {c.risk}</p>
            {c.findings.length > 0 && (
              <p className="text-2xs text-slate-500 font-mono">Resolves: {c.findings.join(", ")}</p>
            )}
          </div>
        ))}

        {recommendations.length > 0 && (
          <div className="space-y-2">
            <h3 className="section-title">Manual follow-up ({recommendations.length})</h3>
            <ul className="space-y-1.5">
              {recommendations.map((r, i) => (
                <li key={i} className="text-xs text-slate-700 p-2.5 rounded-lg border border-slate-200 bg-slate-50">
                  <span className="font-mono font-bold">
                    [{r.severity}] {r.rule_id}
                  </span>{" "}
                  {r.resource && <code className="text-brand-700">{r.resource}</code>} - {r.description}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
