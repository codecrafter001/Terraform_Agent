"use client";

import { useMemo, useState } from "react";
import { Globe2, Info, Search } from "lucide-react";
import { ResourceInventory } from "@/lib/types";

interface InventoryPanelProps {
  inventory?: ResourceInventory;
  scannedRegion: string;
  requestedRegion?: string | null;
}

export default function InventoryPanel({ inventory, scannedRegion, requestedRegion }: InventoryPanelProps) {
  const [query, setQuery] = useState("");
  const [onlySupported, setOnlySupported] = useState(false);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (inventory?.resources ?? []).filter((r) => {
      if (onlySupported && !r.supported) return false;
      if (!q) return true;
      return [r.type, r.region, r.name ?? "", r.arn].some((v) => v.toLowerCase().includes(q));
    });
  }, [inventory, query, onlySupported]);

  if (!inventory || !inventory.available) {
    return (
      <div className="card p-6 flex items-start gap-3.5">
        <div className="p-2 rounded-xl bg-slate-100 text-slate-500 shrink-0">
          <Info className="w-5 h-5" />
        </div>
        <div className="space-y-1.5 text-xs text-slate-600 leading-relaxed">
          <div className="text-sm font-bold text-slate-900">No all-region inventory for this scan</div>
          <p>{inventory?.reason ?? "This job ran before Resource Explorer support was added."}</p>
          <p>
            To enable it: in the AWS console open <strong>Resource Explorer → Settings</strong>, turn it on with an
            aggregator index and a default view, and give the scan credentials{" "}
            <code className="font-mono text-slate-800">resource-explorer-2:ListIndexes</code> and{" "}
            <code className="font-mono text-slate-800">resource-explorer-2:Search</code>. TerraAgent only reads from
            Resource Explorer; it never turns it on or changes it.
          </p>
        </div>
      </div>
    );
  }

  const byRegion = Object.entries(inventory.by_region ?? {});
  const supportedByRegion = inventory.supported_by_region ?? {};
  const maxRegion = Math.max(1, ...byRegion.map(([, n]) => n));
  const byType = Object.entries(inventory.by_type ?? {});
  const total = `${inventory.total ?? 0}${inventory.truncated ? "+" : ""}`;

  return (
    <div className="space-y-5">
      <div className="card p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="flex items-start gap-3">
          <div className="p-2 rounded-xl bg-brand-50 text-brand-600 shrink-0">
            <Globe2 className="w-5 h-5" />
          </div>
          <div>
            <div className="text-sm font-bold text-slate-900">
              {total} resources in {byRegion.length} region{byRegion.length === 1 ? "" : "s"}
            </div>
            <p className="text-xs text-slate-500 mt-0.5">
              {inventory.supported_total ?? 0} are types TerraAgent can generate today ·{" "}
              {inventory.unsupported_total ?? 0} not yet supported ·{" "}
              {inventory.aggregated ? "all regions (aggregator index)" : `${inventory.index_region} only (no aggregator index)`}
            </p>
            <p className="text-xs text-slate-700 mt-1.5">
              Scanned in detail: <span className="font-mono font-semibold">{scannedRegion}</span>
              {requestedRegion === "auto" && <span className="text-slate-500"> (picked automatically)</span>}
            </p>
          </div>
        </div>
        {inventory.truncated && (
          <span className="text-2xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-1 shrink-0">
            Showing the first {inventory.returned} results
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <div className="card overflow-hidden">
          <div className="card-header">
            <h3 className="text-sm font-bold text-slate-900">By region</h3>
            <span className="text-2xs text-slate-500">dark = supported types</span>
          </div>
          <ul className="p-5 space-y-2.5">
            {byRegion.map(([region, n]) => {
              const sup = supportedByRegion[region] ?? 0;
              return (
                <li key={region} className="text-xs">
                  <div className="flex items-center justify-between mb-1">
                    <span className={`font-mono ${region === scannedRegion ? "font-bold text-brand-700" : "text-slate-700"}`}>
                      {region}
                      {region === scannedRegion && <span className="font-sans font-semibold"> · scanned</span>}
                    </span>
                    <span className="tabular-nums text-slate-500">
                      {sup} / {n}
                    </span>
                  </div>
                  <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden flex">
                    <div className="h-full bg-brand-600" style={{ width: `${(sup / maxRegion) * 100}%` }} />
                    <div className="h-full bg-brand-200" style={{ width: `${((n - sup) / maxRegion) * 100}%` }} />
                  </div>
                </li>
              );
            })}
          </ul>
        </div>

        <div className="card overflow-hidden">
          <div className="card-header">
            <h3 className="text-sm font-bold text-slate-900">By resource type</h3>
            <span className="text-2xs text-slate-500">{byType.length} types</span>
          </div>
          <ul className="p-3 max-h-80 overflow-y-auto divide-y divide-slate-100">
            {byType.map(([type, n]) => (
              <li key={type} className="px-2 py-1.5 flex items-center justify-between text-xs">
                <span className="font-mono text-slate-700">{type}</span>
                <span className="tabular-nums text-slate-500">{n}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="card-header flex-col items-stretch sm:flex-row sm:items-center">
          <h3 className="text-sm font-bold text-slate-900">Resources</h3>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-2xs text-slate-600 cursor-pointer whitespace-nowrap">
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-brand-600"
                checked={onlySupported}
                onChange={(e) => setOnlySupported(e.target.checked)}
              />
              Supported only
            </label>
            <div className="relative sm:w-64">
              <Search className="w-3.5 h-3.5 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter by type, region, name..."
                className="field-input pl-9 py-2"
              />
            </div>
          </div>
        </div>
        <div className="max-h-[480px] overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-slate-50">
              <tr className="text-left text-3xs uppercase tracking-wider text-slate-500 border-b border-slate-100">
                <th className="px-5 py-2.5 font-semibold">Name</th>
                <th className="px-5 py-2.5 font-semibold">Type</th>
                <th className="px-5 py-2.5 font-semibold">Region</th>
                <th className="px-5 py-2.5 font-semibold">Terraform</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((r) => (
                <tr key={r.arn} className="hover:bg-slate-50/70" title={r.arn}>
                  <td className="px-5 py-2 text-slate-800 truncate max-w-[16rem]">{r.name || "—"}</td>
                  <td className="px-5 py-2 font-mono text-slate-600">{r.type}</td>
                  <td className="px-5 py-2 font-mono text-slate-600">{r.region}</td>
                  <td className="px-5 py-2">
                    {r.supported ? (
                      <span className="text-3xs font-semibold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">
                        Supported
                      </span>
                    ) : (
                      <span className="text-3xs font-semibold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 border border-slate-200">
                        Not yet
                      </span>
                    )}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-5 py-8 text-center text-slate-500">
                    No resources match this filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
