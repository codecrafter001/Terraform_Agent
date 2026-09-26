"use client";

import { useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Sparkles,
  Layers,
  ArrowRight,
  CheckCircle2,
  AlertTriangle,
  Server,
  Database,
  HardDrive,
  Network,
  Shield,
  Lock,
  Cpu,
  RefreshCw,
  Edit3,
  X,
  Gauge,
  HelpCircle,
  Wrench,
  Check,
} from "lucide-react";
import { IntentAnalysisResult } from "@/lib/types";

interface IntentAnalysisModalProps {
  isOpen: boolean;
  intent: IntentAnalysisResult | null;
  isLoading: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

const OPERATION_BADGES: Record<string, { label: string; color: string; icon: LucideIcon }> = {
  modify: { label: "Modify Infrastructure", color: "bg-blue-100 text-blue-800 border-blue-300", icon: Wrench },
  generate: { label: "Generate IaC Code", color: "bg-emerald-100 text-emerald-800 border-emerald-300", icon: Sparkles },
  scan: { label: "Scan & Inventory", color: "bg-purple-100 text-purple-800 border-purple-300", icon: Layers },
  explain: { label: "Explain Topology", color: "bg-indigo-100 text-indigo-800 border-indigo-300", icon: HelpCircle },
  fix: { label: "Fix / Remediate", color: "bg-rose-100 text-rose-800 border-rose-300", icon: AlertTriangle },
  validate: { label: "Validate & Policy", color: "bg-amber-100 text-amber-800 border-amber-300", icon: CheckCircle2 },
};

const CATEGORY_ICONS: Record<string, LucideIcon> = {
  EC2: Server,
  ECS: Cpu,
  EKS: Cpu,
  RDS: Database,
  S3: HardDrive,
  VPC: Network,
  SG: Lock,
  IAM: Shield,
};

export default function IntentAnalysisModal({
  isOpen,
  intent,
  isLoading,
  onConfirm,
  onCancel,
}: IntentAnalysisModalProps) {
  if (!isOpen || !intent) return null;

  const opKey = (intent.operation || "generate").toLowerCase();
  const badgeConfig = OPERATION_BADGES[opKey] || OPERATION_BADGES.generate;
  const OpIcon = badgeConfig.icon;

  const riskColor =
    intent.risk_level === "high"
      ? "bg-rose-50 text-rose-700 border-rose-200"
      : intent.risk_level === "medium"
      ? "bg-amber-50 text-amber-700 border-amber-200"
      : "bg-emerald-50 text-emerald-700 border-emerald-200";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-xs animate-fade-in">
      <div className="bg-white rounded-2xl shadow-2xl border border-slate-200 max-w-2xl w-full max-h-[90vh] flex flex-col overflow-hidden animate-scale-in">
        {/* Header */}
        <div className="px-6 py-4.5 border-b border-slate-100 flex items-center justify-between bg-gradient-to-r from-slate-50 to-white">
          <div className="flex items-center gap-2.5">
            <div className="p-2 rounded-xl bg-brand-50 text-brand-600 border border-brand-100">
              <Sparkles className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-bold text-slate-900">Intent Analysis Preview</h3>
              <p className="text-2xs text-slate-500 mt-0.5">
                Review analyzed operations and target resources before launching the pipeline
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onCancel}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content Body */}
        <div className="p-6 overflow-y-auto space-y-5">
          {/* Operation & Meta Badges */}
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold border ${badgeConfig.color}`}
            >
              <OpIcon className="w-3.5 h-3.5" />
              {badgeConfig.label}
            </span>

            <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-100 text-slate-700 border border-slate-200">
              Region: <span className="font-mono text-slate-900">{intent.region}</span>
            </span>

            <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-100 text-slate-700 border border-slate-200 capitalize">
              Env: <span className="text-slate-900">{intent.environment}</span>
            </span>

            <span
              className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold border ${riskColor}`}
            >
              Risk: <span className="uppercase font-bold">{intent.risk_level}</span>
            </span>

            <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-brand-50 text-brand-700 border border-brand-200 ml-auto">
              <Gauge className="w-3.5 h-3.5" />
              Confidence: {Math.round(intent.confidence_score * 100)}%
            </span>
          </div>

          {/* Executive Summary */}
          {intent.summary && (
            <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200/80 text-xs text-slate-700 leading-relaxed">
              <span className="font-semibold text-slate-900">Summary: </span>
              {intent.summary}
            </div>
          )}

          {/* Target AWS Resources */}
          <div>
            <div className="text-xs font-bold text-slate-900 mb-2 flex items-center justify-between">
              <span>Target AWS Resources</span>
              <span className="text-2xs text-slate-500 font-normal">
                {intent.target_resources.length} resource(s) identified
              </span>
            </div>

            {intent.target_resources.length === 0 ? (
              <p className="text-2xs text-slate-500 italic p-3 bg-slate-50 rounded-lg">
                No specific resources isolated; broad account discovery will run for selected categories.
              </p>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                {intent.target_resources.map((res, i) => {
                  const CatIcon = CATEGORY_ICONS[res.category] || Server;
                  return (
                    <div
                      key={`${res.resource_type}-${i}`}
                      className="flex items-center gap-3 p-3 rounded-xl border border-slate-200 bg-white shadow-2xs hover:border-brand-300 transition-colors"
                    >
                      <div className="p-2 rounded-lg bg-brand-50 text-brand-600 shrink-0">
                        <CatIcon className="w-4 h-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="text-xs font-bold text-slate-900 truncate">{res.resource_name}</div>
                        <div className="text-3xs font-mono text-slate-500 truncate">{res.resource_type}</div>
                      </div>
                      <span className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-600 text-3xs font-bold">
                        {res.category}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Requested Changes Table */}
          {intent.requested_changes.length > 0 && (
            <div>
              <div className="text-xs font-bold text-slate-900 mb-2">Requested Changes & Parameter Diffs</div>
              <div className="border border-slate-200 rounded-xl overflow-hidden shadow-2xs">
                <table className="w-full text-left text-xs">
                  <thead className="bg-slate-50 border-b border-slate-200 text-slate-500 font-semibold text-3xs uppercase tracking-wider">
                    <tr>
                      <th className="px-3.5 py-2">Resource</th>
                      <th className="px-3.5 py-2">Attribute</th>
                      <th className="px-3.5 py-2">Current</th>
                      <th className="px-3.5 py-2">Target</th>
                      <th className="px-3.5 py-2 text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 bg-white">
                    {intent.requested_changes.map((ch, i) => (
                      <tr key={i} className="hover:bg-slate-50/50">
                        <td className="px-3.5 py-2.5 font-medium text-slate-800">{ch.resource}</td>
                        <td className="px-3.5 py-2.5 font-mono text-2xs text-slate-600">{ch.attribute}</td>
                        <td className="px-3.5 py-2.5 text-2xs font-mono text-slate-500">{ch.current_value}</td>
                        <td className="px-3.5 py-2.5 text-2xs font-mono font-bold text-emerald-600">
                          {ch.target_value}
                        </td>
                        <td className="px-3.5 py-2.5 text-right">
                          <span className="px-2 py-0.5 rounded-md text-3xs font-bold bg-brand-50 text-brand-700 capitalize">
                            {ch.action.replace("_", " ")}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Safety Notice */}
          <div className="p-3 rounded-xl bg-amber-50/70 border border-amber-200/80 text-2xs text-amber-800 flex items-start gap-2.5">
            <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
            <p className="leading-relaxed">
              <span className="font-semibold text-amber-900">Read-Only Safety Guarantee: </span>
              TerraAgent will discover AWS state and synthesize validated Terraform/OpenTofu code without ever mutating
              live cloud resources or executing <code className="font-mono">terraform apply</code>.
            </p>
          </div>
        </div>

        {/* Footer Actions */}
        <div className="px-6 py-4 border-t border-slate-100 bg-slate-50/60 flex items-center justify-between gap-3">
          <button type="button" onClick={onCancel} className="btn-secondary text-xs flex items-center gap-1.5">
            <Edit3 className="w-3.5 h-3.5" />
            Edit Request
          </button>

          <button
            type="button"
            onClick={onConfirm}
            disabled={isLoading}
            className="btn-primary text-xs flex items-center gap-2 py-2.5 px-4 shadow-sm"
          >
            {isLoading ? (
              <>
                <div className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                <span>Launching Pipeline...</span>
              </>
            ) : (
              <>
                <Check className="w-4 h-4" />
                <span>Confirm & Start Pipeline</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
