"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  Loader2,
  Terminal,
  ArrowRight,
  Shield,
  Activity,
  Cpu,
  Layers,
  Sparkles,
  Search,
  CheckCheck,
  FileCode,
  Wrench,
  Tags,
  ClipboardList,
  DollarSign,
  GitCommitHorizontal,
  GitCompare,
  XCircle
} from "lucide-react";
import { fetchJobResults, fetchJobStatus } from "@/lib/api";
import PendingApprovalPanel from "./PendingApprovalPanel";
import { ApprovalDecision, PendingApproval, PlanEquivalenceResult } from "@/lib/types";

interface ScanProgressProps {
  jobId: string;
}

// Must stay in sync with the actual LangGraph node order in
// backend/agents/graph.py::build_graph() - an agent id missing from this list
// breaks the log-driven progress logic below (AGENT_STEPS.findIndex returns
// -1 for an unlisted id, which fails the `idx > 0` check and silently skips
// marking earlier steps done while that agent runs).
const AGENT_STEPS = [
  { id: "intent_router", name: "Intent Router", desc: "Classify request & pipeline parameters", icon: Sparkles },
  { id: "cloud_discovery", name: "Cloud Discovery", desc: "Read-only inventory scan (boto3)", icon: Search },
  { id: "graph_agent", name: "Graph Agent", desc: "Cross-resource DAG adjacency builder", icon: Layers },
  { id: "classification_agent", name: "Classification Agent", desc: "Managed/unmanaged/shared resource triage", icon: Tags },
  { id: "adoption_planning_agent", name: "Adoption Planning Agent", desc: "Migration plan, risk score & import order", icon: ClipboardList },
  { id: "terraform_composer", name: "Terraform Composer", desc: "Local LLM modular HCL synthesis", icon: Cpu },
  { id: "validation_agent", name: "Validation Agent", desc: "fmt, init & sandbox validation", icon: CheckCheck },
  { id: "drift_reconciliation_agent", name: "Drift Reconciliation Agent", desc: "Live AWS attributes vs. generated HCL", icon: GitCompare },
  { id: "plan_equivalence_agent", name: "Plan Equivalence Agent", desc: "Real terraform plan diff vs. live AWS", icon: GitCommitHorizontal },
  { id: "policy_agent", name: "Policy & Security", desc: "tfsec, Checkov, Trivy, OPA policies", icon: Shield },
  { id: "repair_agent", name: "Repair Agent", desc: "Automated syntax & policy error repair", icon: Wrench },
  { id: "cost_agent", name: "Cost Agent", desc: "Infracost monthly cost estimation", icon: DollarSign },
  { id: "documentation_agent", name: "Documentation Agent", desc: "README, import plan & bundle packaging", icon: FileCode },
];

export default function ScanProgress({ jobId }: ScanProgressProps) {
  const router = useRouter();
  const [logs, setLogs] = useState<Array<{ id: number; text: string; agent: string }>>([]);
  const [currentAgent, setCurrentAgent] = useState<string>("intent_router");
  const [completedAgents, setCompletedAgents] = useState<string[]>([]);
  const [isCompleted, setIsCompleted] = useState<boolean>(false);
  const [failureMessage, setFailureMessage] = useState<string | null>(null);
  const [progressPercentage, setProgressPercentage] = useState<number>(0);
  const [isAwaitingApproval, setIsAwaitingApproval] = useState<boolean>(false);
  const [isRejected, setIsRejected] = useState<boolean>(false);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [planEquivalenceResults, setPlanEquivalenceResults] = useState<PlanEquivalenceResult | null>(null);
  const [approvalDecision, setApprovalDecision] = useState<ApprovalDecision | null>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);

  // /status doesn't carry pending_approval/plan_equivalence_results (that's
  // in /results) - fetched once, event-driven, the moment the job actually
  // halts, rather than polled continuously alongside /status. Returns
  // whether it actually succeeded, so the caller only marks this "done" on
  // a real success - otherwise the next 1s poll retries.
  const fetchApprovalDetails = async (): Promise<boolean> => {
    try {
      const results = await fetchJobResults(jobId);
      setPendingApproval(results.pending_approval ?? null);
      setPlanEquivalenceResults(results.plan_equivalence_results ?? null);
      setApprovalDecision(results.approval_decision ?? null);
      return true;
    } catch {
      return false;
    }
  };

  useEffect(() => {
    let cancelled = false;
    // Only true once fetchApprovalDetails has actually SUCCEEDED - unlike a
    // plain "have we seen this status before" flag, this correctly retries
    // on the next 1s poll if the fetch itself failed (a transient network
    // blip), instead of permanently giving up after the first attempt.
    let approvalDetailsFetched = false;

    const checkStatus = async () => {
      try {
        const status = await fetchJobStatus(jobId);
        if (cancelled) return;

        if (status.current_agent) {
          setCurrentAgent(status.current_agent);
        }
        if (status.completed_agents && status.completed_agents.length > 0) {
          setCompletedAgents((prev) => Array.from(new Set([...prev, ...status.completed_agents])));
        }
        if (typeof status.progress_percentage === "number") {
          // Never let a stale/late poll response move the bar backwards -
          // the SSE stream can already have pushed completedAgents further
          // ahead than the last /status snapshot.
          setProgressPercentage((prev) => Math.max(prev, status.progress_percentage));
        }

        if (status.status === "COMPLETE") {
          setCompletedAgents(AGENT_STEPS.map((s) => s.id));
          setIsCompleted(true);
          setIsAwaitingApproval(false);
          setProgressPercentage(100);
        } else if (status.status === "FAILED") {
          setFailureMessage(status.error || "The pipeline failed. See logs for details.");
          setIsAwaitingApproval(false);
        } else if (status.status === "AWAITING_APPROVAL") {
          setIsAwaitingApproval(true);
          if (!approvalDetailsFetched) {
            fetchApprovalDetails().then((ok) => {
              if (ok) approvalDetailsFetched = true;
            });
          }
        } else if (status.status === "REJECTED") {
          setIsRejected(true);
          setIsAwaitingApproval(false);
          if (!approvalDetailsFetched) {
            fetchApprovalDetails().then((ok) => {
              if (ok) approvalDetailsFetched = true;
            });
          }
        } else if (status.status === "RUNNING") {
          // Resumed after an approval - the awaiting-approval panel is no
          // longer the active state, even though pendingApproval/
          // approvalDecision stay populated for the eventual results view.
          setIsAwaitingApproval(false);
        }
      } catch {
        // Retry silently on next interval
      }
    };

    checkStatus();
    const interval = setInterval(checkStatus, 1000);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [jobId]);

  useEffect(() => {
    // SSE Stream Subscription
    const eventSource = new EventSource(`/api/scan/${jobId}/logs`);

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const msg = data.message || event.data;
        const agent = data.agent || "system";

        setLogs((prev) => [...prev, { id: Date.now() + Math.random(), text: msg, agent }]);

        // Extract agent name if tagged [AGENT:name]
        const match = msg.match(/\[AGENT:([a-zA-Z_]+)\]/);
        if (match && match[1]) {
          const detectedAgent = match[1];
          setCurrentAgent(detectedAgent);
          setCompletedAgents((prev) => {
            const idx = AGENT_STEPS.findIndex((s) => s.id === detectedAgent);
            if (idx > 0) {
              const previousSteps = AGENT_STEPS.slice(0, idx).map((s) => s.id);
              return Array.from(new Set([...prev, ...previousSteps]));
            }
            return prev;
          });
        }

        if (msg.includes("COMPLETE") || msg.includes("Output ZIP bundle created")) {
          setCompletedAgents(AGENT_STEPS.map((s) => s.id));
          setIsCompleted(true);
          setProgressPercentage(100);
        } else if (agent === "error" || msg.startsWith("Pipeline error:")) {
          setFailureMessage(msg);
        }
      } catch {
        setLogs((prev) => [...prev, { id: Date.now(), text: event.data, agent: "system" }]);
      }
    };

    eventSource.onerror = () => {
      eventSource.close();
    };

    return () => {
      eventSource.close();
    };
  }, [jobId]);

  // Auto scroll logs
  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [logs]);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
      {(isAwaitingApproval || isRejected || (isCompleted && approvalDecision)) && pendingApproval && (
        <div className="lg:col-span-12">
          <PendingApprovalPanel
            jobId={jobId}
            pendingApproval={pendingApproval}
            planEquivalenceResults={planEquivalenceResults}
            approvalDecision={approvalDecision}
            mode={isAwaitingApproval ? "actionable" : "readonly"}
            onDecision={() => {
              setIsAwaitingApproval(false);
              fetchApprovalDetails();
            }}
          />
        </div>
      )}

      {/* 8-Agent Stepper */}
      <div className="lg:col-span-5 space-y-4">
        <div className="p-6 rounded-2xl border border-slate-200/90 bg-white shadow-sm">
          <div className="flex items-center justify-between pb-4 border-b border-slate-100 mb-5">
            <div className="flex items-center gap-2">
              <Activity className="w-4 h-4 text-brand-600" />
              <h2 className="text-sm font-bold text-slate-900">LangGraph Agent Pipeline</h2>
            </div>
            <span className="text-[11px] px-2.5 py-0.5 rounded-full bg-slate-100 text-slate-700 font-mono font-medium border border-slate-200">
              {jobId.slice(0, 12)}
            </span>
          </div>

          <div className="mb-5">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[11px] font-bold text-slate-600 uppercase tracking-wider">Overall Progress</span>
              <span className="text-xs font-bold text-brand-700 font-mono">{Math.round(progressPercentage)}%</span>
            </div>
            <div className="h-2 rounded-full bg-slate-100 overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ease-out ${
                  failureMessage && !isCompleted
                    ? "bg-rose-500"
                    : "bg-gradient-to-r from-brand-500 to-brand-600"
                }`}
                style={{ width: `${Math.min(100, Math.max(0, progressPercentage))}%` }}
              />
            </div>
          </div>

          <div className="space-y-3.5">
            {AGENT_STEPS.map((step, idx) => {
              const isDone = completedAgents.includes(step.id);
              const isCurrent = currentAgent === step.id && !isDone;

              return (
                <div
                  key={step.id}
                  className={`flex items-start gap-3.5 p-2.5 rounded-xl transition-all ${
                    isCurrent
                      ? "bg-brand-50/80 border border-brand-200/80 shadow-2xs"
                      : isDone
                      ? "bg-slate-50/50"
                      : "opacity-60"
                  }`}
                >
                  <div className="mt-0.5 shrink-0">
                    {isDone ? (
                      <CheckCircle2 className="w-5 h-5 text-emerald-600" />
                    ) : isCurrent ? (
                      <div className="relative">
                        <Loader2 className="w-5 h-5 text-brand-600 animate-spin" />
                      </div>
                    ) : (
                      <Circle className="w-5 h-5 text-slate-300" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <span
                        className={`text-xs font-bold truncate ${
                          isDone
                            ? "text-slate-900"
                            : isCurrent
                            ? "text-brand-900"
                            : "text-slate-500"
                        }`}
                      >
                        {idx + 1}. {step.name}
                      </span>
                      {isCurrent && (
                        <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-brand-600 text-white uppercase tracking-wider animate-pulse shrink-0">
                          Active
                        </span>
                      )}
                    </div>
                    <p className="text-[11px] text-slate-500 truncate mt-0.5">{step.desc}</p>
                  </div>
                </div>
              );
            })}
          </div>

          {failureMessage && !isCompleted && (
            <div className="mt-6 pt-5 border-t border-slate-100 space-y-3">
              <div className="p-4 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800 flex items-start gap-3 shadow-2xs">
                <AlertTriangle className="w-5 h-5 text-rose-600 shrink-0 mt-0.5" />
                <div>
                  <span className="font-bold block text-rose-900">Pipeline Failed</span>
                  <p className="mt-0.5 leading-relaxed">{failureMessage}</p>
                </div>
              </div>
              <button
                onClick={() => router.push("/scan")}
                className="w-full py-3 rounded-xl bg-white hover:bg-slate-50 text-slate-800 border border-slate-200 font-bold flex items-center justify-center gap-2 text-xs shadow-2xs transition-all"
              >
                <span>Start a New Scan</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </button>
            </div>
          )}

          {isRejected && (
            <div className="mt-6 pt-5 border-t border-slate-100 space-y-3">
              <div className="p-4 rounded-xl bg-slate-100 border border-slate-200 text-xs text-slate-700 flex items-start gap-3 shadow-2xs">
                <XCircle className="w-5 h-5 text-slate-500 shrink-0 mt-0.5" />
                <div>
                  <span className="font-bold block text-slate-900">Rejected - Pipeline Halted Permanently</span>
                  <p className="mt-0.5 leading-relaxed">
                    A human rejected the pending findings above. This job will not produce an adoptable bundle.
                  </p>
                </div>
              </div>
              <button
                onClick={() => router.push("/scan")}
                className="w-full py-3 rounded-xl bg-white hover:bg-slate-50 text-slate-800 border border-slate-200 font-bold flex items-center justify-center gap-2 text-xs shadow-2xs transition-all"
              >
                <span>Start a New Scan</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </button>
            </div>
          )}

          {isCompleted && (
            <div className="mt-6 pt-5 border-t border-slate-100">
              <button
                onClick={() => router.push(`/results/${jobId}`)}
                className="w-full py-3.5 rounded-xl bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 active:scale-[0.99] text-white font-bold flex items-center justify-center gap-2 text-xs shadow-lg shadow-emerald-600/25 transition-all"
              >
                <span>View Full Results & Graph</span>
                <ArrowRight className="w-4 h-4" />
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Real-time Streaming Terminal */}
      <div className="lg:col-span-7 flex flex-col h-[580px] rounded-2xl border border-slate-800 bg-[#090d16] shadow-2xl overflow-hidden">
        {/* Terminal Header */}
        <div className="px-4 py-3 bg-[#0f1422] border-b border-slate-800 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5">
              <span className="w-3 h-3 rounded-full bg-rose-500/80 inline-block" />
              <span className="w-3 h-3 rounded-full bg-amber-500/80 inline-block" />
              <span className="w-3 h-3 rounded-full bg-emerald-500/80 inline-block" />
            </div>
            <div className="flex items-center gap-2 text-xs font-mono text-slate-300 font-medium">
              <Terminal className="w-3.5 h-3.5 text-brand-400" />
              <span>Live Agent Stream (SSE)</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
            </span>
            <span className="text-[10px] text-emerald-400 font-mono font-bold tracking-wider">LIVE</span>
          </div>
        </div>

        {/* Logs Scroll Area */}
        <div
          ref={logContainerRef}
          className="flex-1 p-4 font-mono text-xs text-slate-300 overflow-y-auto space-y-1.5 leading-relaxed selection:bg-brand-600 selection:text-white"
        >
          {logs.length === 0 ? (
            <div className="text-slate-500 italic py-4">Connecting to live agent SSE event stream...</div>
          ) : (
            logs.map((l) => (
              <div key={l.id} className="flex items-start gap-2.5 font-mono text-[11px]">
                <span className="text-slate-600 select-none text-[10px] mt-0.5">❯</span>
                <span
                  className={
                    l.text.includes("COMPLETE") || l.text.includes("Passed")
                      ? "text-emerald-400 font-semibold"
                      : l.text.includes("error") || l.text.includes("Failed")
                      ? "text-rose-400 font-semibold"
                      : l.text.includes("[AGENT:")
                      ? "text-brand-300 font-semibold"
                      : "text-slate-300"
                  }
                >
                  {l.text}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
