import Link from "next/link";
import { Cloud, PlusCircle, Rocket, UploadCloud } from "lucide-react";
import { fetchDeployments } from "@/lib/api";
import { PageHeader, StatusBadge } from "@/components/ui";
import { TARGET_LABELS } from "@/lib/deployments";
import ZipQuickUpload from "@/components/ZipQuickUpload";

export const dynamic = "force-dynamic";

export default async function DeploymentsPage() {
  const deployments = await fetchDeployments(100);

  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Deployments" }]}
        title="Deployments"
        description="Projects analysed, built and verified for deployment to AWS."
        actions={
          <>
            <Link href="/deployments/targets" className="btn-secondary">
              <Cloud className="w-4 h-4" />
              AWS accounts
            </Link>
            <Link href="/deploy" className="btn-primary">
              <PlusCircle className="w-4 h-4" />
              New deployment
            </Link>
          </>
        }
      />

      {/* Quick Drag & Drop ZIP Upload Card */}
      <div className="card p-4 sm:p-5 shadow-xs">
        <div className="flex items-center gap-2 font-bold text-xs text-slate-800 mb-3">
          <UploadCloud className="w-4 h-4 text-brand-600" />
          Quick Upload & Deploy Application (.zip)
        </div>
        <ZipQuickUpload />
      </div>
      {deployments.length === 0 ? (
        <div className="card p-10 text-center text-xs text-slate-500 space-y-2">
          <Rocket className="w-6 h-6 mx-auto text-slate-400" />
          <p>No deployments yet. Upload a project or connect a GitHub repository to start.</p>
        </div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-3xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="text-left px-4 py-2.5">Deployment</th>
                <th className="text-left px-4 py-2.5">Source</th>
                <th className="text-left px-4 py-2.5">Target</th>
                <th className="text-left px-4 py-2.5">Status</th>
                <th className="text-left px-4 py-2.5">Region</th>
                <th className="text-left px-4 py-2.5">Created</th>
              </tr>
            </thead>
            <tbody>
              {deployments.map((d) => (
                <tr key={d.id} className="border-t border-slate-100 hover:bg-slate-50/60">
                  <td className="px-4 py-2.5 font-mono">
                    <Link href={`/deployments/${d.id}`} className="text-brand-700 hover:underline">{d.id}</Link>
                  </td>
                  <td className="px-4 py-2.5 max-w-[16rem] truncate" title={d.source_name}>{d.source_name}</td>
                  <td className="px-4 py-2.5">{d.target_type ? TARGET_LABELS[d.target_type] : "—"}</td>
                  <td className="px-4 py-2.5"><StatusBadge status={d.status} /></td>
                  <td className="px-4 py-2.5">{d.region}</td>
                  <td className="px-4 py-2.5 text-slate-500 whitespace-nowrap">{new Date(d.created_at + "Z").toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
