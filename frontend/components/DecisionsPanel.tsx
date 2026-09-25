"use client";

import { useMemo, useState } from "react";
import { AdoptionDecision, InfraModel } from "@/lib/types";

const DECISIONS: { id: AdoptionDecision; label: string; hint: string; cls: string; chip: string }[] = [
  {
    id: "manage",
    label: "Manage",
    hint: "resource + import block",
    cls: "text-emerald-700",
    chip: "bg-emerald-50 text-emerald-700 border-emerald-200",
  },
  {
    id: "reference",
    label: "Reference",
    hint: "owned elsewhere: data block",
    cls: "text-blue-700",
    chip: "bg-blue-50 text-blue-700 border-blue-200",
  },
  {
    id: "exclude",
    label: "Exclude",
    hint: "AWS defaults, other IaC, unsupported",
    cls: "text-slate-600",
    chip: "bg-slate-100 text-slate-600 border-slate-200",
  },
  {
    id: "review",
    label: "Review",
    hint: "unclear ownership: needs a human",
    cls: "text-amber-700",
    chip: "bg-amber-50 text-amber-800 border-amber-200",
  },
];

export default function DecisionsPanel({ model }: { model?: InfraModel }) {
  const [filter, setFilter] = useState<AdoptionDecision | "all">("all");

  const rows = useMemo(
    () => (model?.records ?? []).filter((r) => filter === "all" || r.decision === filter),
    [model, filter]
  );

  if (!model || !model.records?.length) return null;
  const chip = Object.fromEntries(DECISIONS.map((d) => [d.id, d])) as Record<AdoptionDecision, (typeof DECISIONS)[number]>;

  return (
    <div className="card overflow-hidden">
      <div className="card-header">
        <div>
          <h2 className="text-sm font-bold text-slate-900">Classification decisions</h2>
          <p className="text-2xs text-slate-500 mt-0.5">
            Rules first: tags, other-IaC ownership, AWS defaults and the dependency graph. {model.summary.total} resources.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-px bg-slate-100 border-b border-slate-100">
        {DECISIONS.map((d) => (
          <button
            key={d.id}
            onClick={() => setFilter(filter === d.id ? "all" : d.id)}
            aria-pressed={filter === d.id}
            className={`bg-white px-4 py-3 text-left transition-colors hover:bg-slate-50 ${
              filter === d.id ? "ring-2 ring-inset ring-brand-500/30" : ""
            }`}
          >
            <div className={`text-xl font-bold tabular-nums ${d.cls}`}>{model.summary[d.id] ?? 0}</div>
            <div className="text-3xs uppercase tracking-wider font-semibold text-slate-500 mt-0.5">{d.label}</div>
            <div className="text-3xs text-slate-400 mt-0.5">{d.hint}</div>
          </button>
        ))}
      </div>

      <div className="max-h-[420px] overflow-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-slate-50">
            <tr className="text-left text-3xs uppercase tracking-wider text-slate-500 border-b border-slate-100">
              <th className="px-5 py-2.5 font-semibold">Resource</th>
              <th className="px-5 py-2.5 font-semibold">Decision</th>
              <th className="px-5 py-2.5 font-semibold">Why</th>
              <th className="px-5 py-2.5 font-semibold">Import ID</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {rows.map((r) => (
              <tr key={r.id} className="align-top hover:bg-slate-50/70">
                <td className="px-5 py-2">
                  <div className="font-mono text-slate-800">{r.type}</div>
                  <div className="font-mono text-2xs text-slate-500">{r.name || r.id}</div>
                </td>
                <td className="px-5 py-2">
                  <span className={`text-3xs font-semibold px-2 py-0.5 rounded-full border ${chip[r.decision]?.chip ?? ""}`}>
                    {chip[r.decision]?.label ?? r.decision}
                  </span>
                </td>
                <td className="px-5 py-2 text-slate-600 max-w-md">
                  {r.reasons[0]}
                  {r.evidence?.source_api && (
                    <div className="text-3xs text-slate-400 mt-0.5 font-mono">
                      {r.evidence.source_api}
                      {r.evidence.rule ? ` · rule ${r.evidence.rule}` : ""}
                    </div>
                  )}
                </td>
                <td className="px-5 py-2 font-mono text-2xs text-slate-600">{r.import_id ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
