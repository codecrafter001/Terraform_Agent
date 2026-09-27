import Link from "next/link";
import { FileText, PlusCircle, ArrowRight, Sparkles, CheckCircle2 } from "lucide-react";
import { PageHeader } from "@/components/ui";
import SampleDataBanner from "@/components/SampleDataBanner";

export default function ChangeRequestsPage() {
  const sampleRequests = [
    {
      id: "req-20241218-001",
      title: "Scale EC2 & Fargate backend tasks",
      prompt: "Increase the EC2 web server from t2.micro to t2.medium and scale the Fargate backend service from 2 to 4 tasks.",
      status: "Ready for Review",
      region: "ap-south-1",
      updated: "Just now",
      jobId: "job-d276dd5537c3",
    },
    {
      id: "req-20241217-004",
      title: "S3 Bucket Encryption & Public Access Block",
      prompt: "Enforce default AES256 server-side encryption and enable all public access block settings.",
      status: "Merged",
      region: "us-east-1",
      updated: "1 day ago",
      jobId: "job-s3-encryption",
    },
  ];

  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Change Requests" }]}
        title={
          <div className="flex items-center gap-3">
            <FileText className="w-6 h-6 text-brand-600" />
            <span>DevOps Change Requests</span>
          </div>
        }
        meta="Natural-language infrastructure modification requests analyzed by the 4-agent engine."
        actions={
          <Link href="/scan" className="btn-primary">
            <PlusCircle className="w-4 h-4" />
            New Change Request
          </Link>
        }
      />

      <SampleDataBanner feature="Change Requests" />

      <div className="space-y-4">
        {sampleRequests.map((req) => (
          <div
            key={req.id}
            className="card p-5 hover:border-brand-300 transition-all space-y-3"
          >
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
              <div className="flex items-center gap-2.5">
                <span className="font-bold text-sm text-slate-900">{req.title}</span>
                <span
                  className={`text-2xs font-semibold px-2 py-0.5 rounded-full border ${
                    req.status === "Ready for Review"
                      ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                      : "bg-blue-50 text-blue-700 border-blue-200"
                  }`}
                >
                  {req.status}
                </span>
              </div>
              <span className="text-2xs text-slate-400 font-mono">{req.region} · {req.updated}</span>
            </div>

            <p className="text-xs text-slate-600 italic bg-slate-50 p-3 rounded-xl border border-slate-100">
              &ldquo;{req.prompt}&rdquo;
            </p>

            <div className="flex items-center justify-between pt-2 border-t border-slate-100 text-xs">
              <span className="text-2xs font-mono text-slate-500">ID: {req.id}</span>
              <Link
                href={`/results/${req.jobId}`}
                className="font-bold text-brand-600 hover:text-brand-700 flex items-center gap-1"
              >
                View Diff & Changes
                <ArrowRight className="w-3.5 h-3.5" />
              </Link>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
