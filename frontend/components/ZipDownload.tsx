"use client";

import { useState } from "react";
import { AlertTriangle, Check, ChevronDown, Copy, Download, FileCode, ShieldCheck, FolderArchive } from "lucide-react";
import { JobStatus, ZipManifestEntry } from "@/lib/types";

interface ZipDownloadProps {
  jobId: string;
  downloadUrl?: string;
  manifest?: ZipManifestEntry[];
  sha256?: string;
  // A human explicitly rejected this job (POST /scan/{job_id}/reject) - the
  // ZIP still exists as an audit record, but must never carry the same
  // "Validated & Security Checked, ready to adopt" framing as a normal or
  // approved bundle. Found live: this component had zero status awareness,
  // so a rejected job's results page showed the exact same green badge and
  // active download button as an approved one.
  status?: JobStatus;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export default function ZipDownload({ jobId, downloadUrl, manifest = [], sha256, status }: ZipDownloadProps) {
  const url = downloadUrl || `/api/download/${jobId}`;
  const [showContents, setShowContents] = useState(false);
  const [copied, setCopied] = useState(false);
  const isRejected = status === "REJECTED";

  const copyChecksum = () => {
    if (!sha256) return;
    navigator.clipboard.writeText(sha256);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div
      className={`rounded-2xl border shadow-sm overflow-hidden ${
        isRejected
          ? "border-slate-300 bg-slate-50"
          : "border-brand-200/90 bg-gradient-to-r from-blue-50/50 via-indigo-50/30 to-white"
      }`}
    >
      <div className="p-6 sm:p-7 flex flex-col sm:flex-row items-center justify-between gap-6">
        <div className="space-y-2 text-center sm:text-left">
          {isRejected ? (
            <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-amber-50 text-amber-800 text-xs font-bold border border-amber-200/80 shadow-2xs">
              <AlertTriangle className="w-3.5 h-3.5 text-amber-600" />
              <span>Rejected - Audit Record Only, Not Adoption-Ready</span>
            </div>
          ) : (
            <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-emerald-50 text-emerald-700 text-xs font-bold border border-emerald-200/80 shadow-2xs">
              <ShieldCheck className="w-3.5 h-3.5 text-emerald-600" />
              <span>Validated & Security Checked</span>
            </div>
          )}
          <h3 className="text-xl font-bold text-slate-900 flex items-center justify-center sm:justify-start gap-2.5">
            <div className={`p-2 rounded-xl text-white shadow-xs ${isRejected ? "bg-slate-500" : "bg-brand-600"}`}>
              <FolderArchive className="w-5 h-5" />
            </div>
            <span>Download Terraform Bundle (.ZIP)</span>
          </h3>
          <p className="text-xs text-slate-500 max-w-xl leading-relaxed">
            {isRejected ? (
              <>A human rejected this configuration - this bundle is kept only as a record of what was generated and why. Do not run the import or apply steps inside it.</>
            ) : (
              <>Includes <code className="text-slate-800 bg-white px-1.5 py-0.5 rounded border border-slate-200 font-mono">resources.tf</code>, <code className="text-slate-800 bg-white px-1.5 py-0.5 rounded border border-slate-200 font-mono">variables.tf</code>, <code className="text-slate-800 bg-white px-1.5 py-0.5 rounded border border-slate-200 font-mono">providers.tf</code>, dependency graph JSON, security reports, and import guides.</>
            )}
          </p>
        </div>

        <a
          href={url}
          download={`terraagent_${jobId}.zip`}
          className={`px-7 py-3.5 rounded-xl font-bold text-sm shadow-lg flex items-center gap-2 transition-all group shrink-0 ${
            isRejected
              ? "bg-slate-600 hover:bg-slate-500 text-white shadow-slate-500/25"
              : "bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 active:scale-[0.98] text-white shadow-brand-500/25"
          }`}
        >
          <Download className="w-4 h-4 group-hover:-translate-y-0.5 transition-transform" />
          <span>{isRejected ? "Download Audit Copy (.ZIP)" : "Download Bundle (.ZIP)"}</span>
        </a>
      </div>

      {(manifest.length > 0 || sha256) && (
        <div className="border-t border-brand-100/80 bg-white/80 backdrop-blur-xs">
          {manifest.length > 0 && (
            <button
              onClick={() => setShowContents((v) => !v)}
              className="w-full px-6 py-3 flex items-center justify-between text-xs font-bold text-slate-700 hover:text-slate-900 hover:bg-slate-50/50 transition-colors"
            >
              <div className="flex items-center gap-2">
                <FileCode className="w-4 h-4 text-brand-600" />
                <span>Bundle Contents ({manifest.length} verified files)</span>
              </div>
              <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform ${showContents ? "rotate-180" : ""}`} />
            </button>
          )}

          {showContents && manifest.length > 0 && (
            <div className="px-6 pb-4 pt-1 max-h-56 overflow-y-auto space-y-1.5 border-t border-slate-100">
              {manifest.map((entry) => (
                <div key={entry.name} className="flex items-center justify-between text-xs font-mono py-1 px-2.5 rounded-lg bg-slate-50/80 hover:bg-slate-100/80 transition-colors">
                  <span className="text-slate-700 truncate font-medium">{entry.name}</span>
                  <span className="text-slate-400 text-[11px] shrink-0 ml-3">{formatBytes(entry.size)}</span>
                </div>
              ))}
            </div>
          )}

          {sha256 && (
            <div className="px-6 py-3 border-t border-brand-100/60 bg-slate-50/50 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <span className="text-[10px] uppercase tracking-wider text-slate-500 font-bold">
                  SHA-256 Checksum
                </span>
                <div className="font-mono text-xs text-slate-700 truncate font-medium">{sha256}</div>
              </div>
              <button
                onClick={copyChecksum}
                className="px-2.5 py-1.5 rounded-lg bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 hover:text-slate-900 transition-colors shrink-0 flex items-center gap-1.5 text-xs font-semibold shadow-2xs"
                title="Copy checksum"
              >
                {copied ? <Check className="w-3.5 h-3.5 text-emerald-600" /> : <Copy className="w-3.5 h-3.5 text-slate-500" />}
                <span>{copied ? "Copied" : "Copy"}</span>
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
