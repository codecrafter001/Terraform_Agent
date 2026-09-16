"use client";

import { useState } from "react";
import { CheckCircle2, XCircle, ShieldAlert, FileText, Copy, Check, Terminal, ShieldCheck } from "lucide-react";
import { SecurityFinding, SecurityReport, ValidationCheck, ValidationReportData } from "@/lib/types";

interface DiscoveredResource {
  resource_type?: string;
  id?: string;
  name?: string;
}

interface ValidationReportProps {
  validationResults: ValidationReportData;
  securityResults: SecurityReport;
  resources: DiscoveredResource[];
}

export default function ValidationReport({
  validationResults,
  securityResults,
  resources,
}: ValidationReportProps) {
  const [activeTab, setActiveTab] = useState<"validation" | "security" | "import">("validation");
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);

  const copyToClipboard = (text: string, idx: number) => {
    navigator.clipboard.writeText(text);
    setCopiedIdx(idx);
    setTimeout(() => setCopiedIdx(null), 2000);
  };

  const findings = securityResults?.findings || [];
  const checks = validationResults?.checks || [];

  return (
    <div className="rounded-2xl border border-slate-200/90 bg-white shadow-sm overflow-hidden">
      {/* Header & Segmented Tabs */}
      <div className="p-4 sm:p-5 border-b border-slate-100 bg-slate-50/70 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h3 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-brand-600" />
            <span>Automated Verification & Policy Auditing</span>
          </h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Validation tests, multi-engine security scans (tfsec, Checkov, OPA), and import instructions.
          </p>
        </div>

        {/* Segmented Tab Controls */}
        <div className="flex bg-slate-200/70 p-1 rounded-xl gap-1 shrink-0 self-start sm:self-auto">
          <button
            onClick={() => setActiveTab("validation")}
            className={`px-3.5 py-1.5 text-xs font-bold rounded-lg flex items-center gap-1.5 transition-all ${
              activeTab === "validation"
                ? "bg-white text-slate-900 shadow-xs"
                : "text-slate-600 hover:text-slate-900"
            }`}
          >
            <FileText className="w-3.5 h-3.5" />
            <span>Validation ({checks.length})</span>
          </button>

          <button
            onClick={() => setActiveTab("security")}
            className={`px-3.5 py-1.5 text-xs font-bold rounded-lg flex items-center gap-1.5 transition-all ${
              activeTab === "security"
                ? "bg-white text-slate-900 shadow-xs"
                : "text-slate-600 hover:text-slate-900"
            }`}
          >
            <ShieldAlert className="w-3.5 h-3.5" />
            <span>Security ({findings.length})</span>
          </button>

          <button
            onClick={() => setActiveTab("import")}
            className={`px-3.5 py-1.5 text-xs font-bold rounded-lg flex items-center gap-1.5 transition-all ${
              activeTab === "import"
                ? "bg-white text-slate-900 shadow-xs"
                : "text-slate-600 hover:text-slate-900"
            }`}
          >
            <Terminal className="w-3.5 h-3.5" />
            <span>Import Plan ({resources.length})</span>
          </button>
        </div>
      </div>

      {/* Tab Body */}
      <div className="p-5 sm:p-6">
        {/* 1. Validation Checks */}
        {activeTab === "validation" && (
          <div className="space-y-4">
            {checks.length === 0 ? (
              <div className="p-8 text-center text-slate-400 text-xs italic">
                No validation checks recorded for this job.
              </div>
            ) : (
              checks.map((c: ValidationCheck, i: number) => (
                <div
                  key={i}
                  className="p-4 rounded-xl border border-slate-200/80 bg-slate-50/50 space-y-2.5 shadow-2xs"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs font-bold text-slate-800 bg-white px-2.5 py-1 rounded-md border border-slate-200">
                      {c.check_name}
                    </span>
                    {c.passed ? (
                      <span className="inline-flex items-center gap-1 text-xs font-bold text-emerald-700 bg-emerald-50 px-2.5 py-0.5 rounded-full border border-emerald-200 shadow-2xs">
                        <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" /> Passed
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs font-bold text-rose-700 bg-rose-50 px-2.5 py-0.5 rounded-full border border-rose-200 shadow-2xs">
                        <XCircle className="w-3.5 h-3.5 text-rose-600" /> Failed
                      </span>
                    )}
                  </div>
                  {c.output && (
                    <pre className="p-3.5 rounded-xl text-xs font-mono overflow-x-auto bg-slate-900 text-slate-200 leading-relaxed shadow-inner">
                      {c.output}
                    </pre>
                  )}
                </div>
              ))
            )}
          </div>
        )}

        {/* 2. Security Findings */}
        {activeTab === "security" && (
          <div className="space-y-4">
            {securityResults?.compliance_summary?.checkov && (
              <div className="p-3.5 rounded-xl border border-slate-200 bg-slate-50 flex flex-wrap items-center gap-4 text-xs font-medium">
                <span className="font-bold text-slate-800">Checkov Compliance Summary:</span>
                <span className="text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
                  {securityResults.compliance_summary.checkov.passed} passed
                </span>
                <span className="text-rose-700 bg-rose-50 px-2 py-0.5 rounded border border-rose-200">
                  {securityResults.compliance_summary.checkov.failed} failed
                </span>
                <span className="text-slate-500 bg-slate-100 px-2 py-0.5 rounded border border-slate-200">
                  {securityResults.compliance_summary.checkov.skipped} skipped
                </span>
              </div>
            )}

            {findings.length === 0 ? (
              <div className="text-center py-10 text-emerald-700 bg-emerald-50/50 rounded-xl border border-emerald-100 flex flex-col items-center gap-2">
                <CheckCircle2 className="w-8 h-8 text-emerald-600" />
                <span className="text-sm font-bold">All Security Policies Passed Cleanly</span>
                <span className="text-xs text-slate-500 max-w-sm">
                  Zero critical or high severity vulnerabilities discovered across tfsec, Checkov, and OPA rules.
                </span>
              </div>
            ) : (
              findings.map((f: SecurityFinding, i: number) => (
                <div
                  key={i}
                  className="p-4 rounded-xl border border-slate-200/90 bg-white flex items-start justify-between gap-4 shadow-2xs hover:border-slate-300 transition-colors"
                >
                  <div className="space-y-1.5 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-xs font-bold text-slate-800 bg-slate-100 px-2 py-0.5 rounded border border-slate-200">
                        [{f.tool?.toUpperCase()}] {f.rule_id}
                      </span>
                      <span
                        className={`text-[10px] font-bold px-2 py-0.5 rounded-full uppercase border shadow-2xs ${
                          f.severity === "CRITICAL"
                            ? "bg-rose-50 text-rose-700 border-rose-200"
                            : f.severity === "HIGH"
                            ? "bg-amber-50 text-amber-700 border-amber-200"
                            : "bg-blue-50 text-blue-700 border-blue-200"
                        }`}
                      >
                        {f.severity}
                      </span>
                    </div>
                    <p className="text-xs text-slate-700 leading-relaxed font-normal">{f.description}</p>
                    {f.resource && (
                      <div className="text-[11px] font-mono text-slate-500">
                        Target Resource: <code className="text-brand-700">{f.resource}</code>
                      </div>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        )}

        {/* 3. Import Plan */}
        {activeTab === "import" && (
          <div className="space-y-3">
            <div className="p-3 rounded-xl bg-blue-50/70 border border-blue-200/60 text-xs text-blue-900 mb-4">
              <span className="font-bold">Human-in-the-Loop Safe Imports:</span> Execute these commands locally in your terminal to bind live AWS resources to your generated Terraform state without modifying the live cloud infrastructure.
            </div>

            {resources.length === 0 ? (
              <div className="p-8 text-center text-slate-400 text-xs italic">
                No discovered resources to generate import commands for.
              </div>
            ) : (
              resources.map((res: DiscoveredResource, idx: number) => {
                const cleanName = (res.name || res.id || "resource").replace(/[-.]/g, "_").toLowerCase();
                const cmd = `terraform import ${res.resource_type}.${cleanName} ${res.id}`;

                return (
                  <div
                    key={idx}
                    className="p-3 rounded-xl bg-slate-900 border border-slate-800 flex items-center justify-between gap-3 shadow-sm group"
                  >
                    <code className="text-xs font-mono text-emerald-400 truncate select-all">{cmd}</code>
                    <button
                      onClick={() => copyToClipboard(cmd, idx)}
                      className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition-colors shrink-0 flex items-center gap-1 text-[11px] font-medium"
                      title="Copy command"
                    >
                      {copiedIdx === idx ? (
                        <>
                          <Check className="w-3.5 h-3.5 text-emerald-400" />
                          <span className="text-emerald-400 text-[10px]">Copied</span>
                        </>
                      ) : (
                        <>
                          <Copy className="w-3.5 h-3.5" />
                          <span className="text-[10px]">Copy</span>
                        </>
                      )}
                    </button>
                  </div>
                );
              })
            )}
          </div>
        )}
      </div>
    </div>
  );
}
