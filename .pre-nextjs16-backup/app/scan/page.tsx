import CredentialForm from "@/components/CredentialForm";
import { Sparkles, ArrowLeft } from "lucide-react";
import Link from "next/link";

export default function ScanLauncherPage() {
  return (
    <div className="max-w-3xl mx-auto px-4 py-8 space-y-6">
      <div className="space-y-2">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-900 transition-colors mb-1 font-medium"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          Back to Dashboard
        </Link>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900 flex items-center gap-2.5">
          <div className="p-2 rounded-xl bg-brand-50 text-brand-600 border border-brand-200/60 shadow-2xs">
            <Sparkles className="w-5 h-5" />
          </div>
          <span>Launch Cloud Discovery & Synthesis</span>
        </h1>
        <p className="text-slate-500 text-sm">
          Execute zero-mutation AWS resource discovery and multi-agent Terraform/OpenTofu HCL generation.
        </p>
      </div>

      <div className="rounded-2xl border border-slate-200/90 bg-white p-6 sm:p-8 shadow-sm">
        <CredentialForm />
      </div>
    </div>
  );
}
