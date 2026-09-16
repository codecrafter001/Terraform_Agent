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
  Tags
} from "lucide-react";
import { fetchJobStatus } from "@/lib/api";

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
  { id: "terraform_composer", name: "Terraform Composer", desc: "Local LLM modular HCL synthesis", icon: Cpu },
  { id: "validation_agent", name: "Validation Agent", desc: "fmt, init & sandbox validation", icon: CheckCheck },
  { id: "policy_agent", name: "Policy & Security", desc: "tfsec, Checkov, Trivy, OPA policies", icon: Shield },
  { id: "repair_agent", name: "Repair Agent", desc: "Automated syntax & policy error repair", icon: Wrench },
  { id: "documentation_agent", name: "Documentation Agent", desc: "README, import plan & bundle packaging", icon: FileCode },
];

export default function ScanProgress({ jobId }: ScanProgressProps) {
  const router = useRouter();
  const [logs, setLogs] = useState<Array<{ id: number; text: string; agent: string }>>([]);
  const [currentAgent, setCurrentAgent] = useState<string>("intent_router");
  const [completedAgents, setCompletedAgents] = useState<string[]>([]);
  const [isCompleted, setIsCompleted] = useState<boolean>(false);
  const [failureMessage, setFailureMessage] = useState<string | null>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;

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

        if (status.status === "COMPLETE") {
          setCompletedAgents(AGENT_STEPS.map((s) => s.id));
          setIsCompleted(true);
        } else if (status.status === "FAILED") {
          setFailureMessage(status.error || "The pipeline failed. See logs for details.");
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
