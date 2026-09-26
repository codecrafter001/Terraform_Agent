import { Info } from "lucide-react";
import { IntentAnalysisResult } from "@/lib/types";

/**
 * Shown when a job came from a natural-language change request. Change
 * Request PRs are Phase 2 (docs/design/phase2-change-requests): this run did
 * a normal zero-change adoption, so no diff for the requested changes exists
 * yet - this says so instead of showing an illustrative one.
 */
export default function ChangeRequestNotice({
  operation,
  userRequest,
  intent,
}: {
  operation?: string;
  userRequest?: string | null;
  intent?: IntentAnalysisResult | null;
}) {
  const changes = intent?.requested_changes ?? [];
  const isChange = operation === "modify" || operation === "fix" || changes.length > 0;
  if (!isChange) return null;

  return (
    <div className="card p-5 space-y-3 border-blue-200 bg-blue-50/40">
      <div className="flex items-start gap-3">
        <Info className="w-5 h-5 text-blue-600 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <h2 className="text-sm font-bold text-slate-900">Your requested changes are not applied in this run</h2>
          <p className="text-xs text-slate-600">
            This scan adopted your infrastructure exactly as it is today (zero changes). Turning a request into a
            Change Request PR comes next: once the adoption PR is merged, the requested edits are made on top of it
            and verified against a plan.
          </p>
        </div>
      </div>
      {userRequest && <p className="text-xs text-slate-700 italic">&ldquo;{userRequest}&rdquo;</p>}
      {changes.length > 0 && (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-3xs uppercase tracking-wider text-slate-500">
              <th className="py-1 pr-2">Resource</th>
              <th className="py-1 pr-2">Attribute</th>
              <th className="py-1 pr-2">Now</th>
              <th className="py-1">Requested</th>
            </tr>
          </thead>
          <tbody>
            {changes.map((c, i) => (
              <tr key={i} className="border-t border-blue-100">
                <td className="py-1.5 pr-2 font-medium text-slate-800">{c.resource}</td>
                <td className="py-1.5 pr-2 font-mono">{c.attribute}</td>
                <td className="py-1.5 pr-2 font-mono text-slate-500">{c.current_value || "—"}</td>
                <td className="py-1.5 font-mono text-slate-900">{c.target_value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
