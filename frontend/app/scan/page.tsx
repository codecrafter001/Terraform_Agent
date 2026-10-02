import Link from "next/link";
import { Rocket, Sparkles, ArrowRight } from "lucide-react";
import CredentialForm from "@/components/CredentialForm";
import ZipQuickUpload from "@/components/ZipQuickUpload";
import { PageHeader } from "@/components/ui";

export default function ScanLauncherPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "New scan" }]}
        title="New Scan & Discovery"
        description="Discover AWS resources read-only and generate validated Terraform or OpenTofu code."
      />

      {/* Mode Switcher Banner */}
      <div className="p-4 rounded-2xl bg-gradient-to-r from-brand-50 to-indigo-50/60 border border-brand-200/80 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 shadow-xs">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-brand-600 text-white flex items-center justify-center shrink-0 shadow-xs">
            <Rocket className="w-5 h-5" />
          </div>
          <div>
            <div className="text-xs font-bold text-slate-900 flex items-center gap-1.5">
              Looking to deploy application code to AWS?
              <span className="px-1.5 py-0.5 rounded-full text-3xs font-semibold bg-brand-100 text-brand-700">New</span>
            </div>
            <p className="text-2xs text-slate-600 mt-0.5">
              Upload a ZIP archive or connect a GitHub repo to automatically build and provision Static Sites, Lambdas, or ECS Containers.
            </p>
          </div>
        </div>
        <Link
          href="/deploy"
          className="btn-secondary shrink-0 text-xs py-2 px-3.5 shadow-xs whitespace-nowrap"
        >
          Use a GitHub repo
          <ArrowRight className="w-3.5 h-3.5" />
        </Link>
      </div>

      <div className="card p-4 sm:p-5 -mt-3">
        <ZipQuickUpload />
      </div>

      <CredentialForm />
    </div>
  );
}
