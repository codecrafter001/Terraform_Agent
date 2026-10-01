"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { LucideIcon } from "lucide-react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Circle,
  Cpu,
  Loader2,
  Package,
  PauseCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  Terminal,
  Wrench,
  XCircle,
} from "lucide-react";
import { fetchJobResults, fetchJobStatus } from "@/lib/api";
import PendingApprovalPanel from "./PendingApprovalPanel";
import {
  ApprovalDecision,
  ApprovalRequest,
  JobProgress,
  JobResults,
  PendingApproval,
  PlanEquivalenceResult,
  RepairEntry,
  StageId,
  VerificationIteration,
} from "@/lib/types";

interface ScanProgressProps {
  jobId: string;
}

interface StageDef {
  id: StageId;
  name: string;
  desc: string;
  icon: LucideIcon;
  usesLlm: boolean;
  // Underlying step ids (backend/agents/graph.py) - used to show which tool
  // the agent is currently running.
  steps: { id: string; label: string }[];
}

// Must stay in sync with backend/agents/graph.py::ALL_STAGES and the steps
// each stage node runs.
const STAGES: StageDef[] = [
  {
    id: "infrastructure",
    name: "Infrastructure Agent",
    desc: "Read-only discovery, dependency graph and ownership classification",
    icon: Search,
    usesLlm: false,
    steps: [
      { id: "intent_router", label: "Request routing" },
      { id: "resource_explorer", label: "All-region inventory (Resource Explorer)" },
      { id: "cloud_discovery", label: "AWS discovery (Describe/Get/List)" },
      { id: "graph_agent", label: "Dependency graph" },
      { id: "classification_agent", label: "Ownership classification" },
    ],
  },
  {
    id: "iac_engineering",
    name: "IaC Engineering Agent",
    desc: "Adoption plan and Terraform generation; repairs blocks that fail validation",
    icon: Cpu,
    usesLlm: true,
    steps: [
      { id: "adoption_planning_agent", label: "Adoption plan & import order" },
      { id: "terraform_composer", label: "HCL generation" },
      { id: "repair_agent", label: "Repair (invariant-checked)" },
    ],
  },
  {
    id: "verification",
    name: "Verification & Risk Agent",
    desc: "Judges only, never edits. Validate first, then drift, plan and policy",
    icon: ShieldCheck,
    usesLlm: false,
    steps: [
      { id: "validation_agent", label: "fmt / init / validate" },
      { id: "drift_reconciliation_agent", label: "Drift vs. live AWS" },
      { id: "plan_equivalence_agent", label: "Plan equivalence" },
      { id: "policy_agent", label: "Checkov · Trivy · OPA (report only)" },
    ],
  },
  {
    id: "delivery",
    name: "Delivery & Approval Agent",
    desc: "Risk gate for human approval, then docs, import plan and bundle",
    icon: Package,
    usesLlm: false,
    steps: [
      { id: "awaiting_approval", label: "Risk gate" },
      { id: "cost_agent", label: "Cost estimate" },
      { id: "documentation_agent", label: "Docs, import plan & bundle" },
    ],
  },
];

// Log line tag -> stage, for coloring the live log. Covers both the agents'
// own lines and the underlying steps' [AGENT:step] lines.
const STEP_TO_STAGE: Record<string, StageId> = Object.fromEntries(
  STAGES.flatMap((s) => [[s.id, s.id], ...s.steps.map((st) => [st.id, s.id])])
);

const STAGE_TAG: Record<StageId | "system", { label: string; cls: string }> = {
  infrastructure: { label: "infra", cls: "text-sky-300" },
  iac_engineering: { label: "iac", cls: "text-violet-300" },
  verification: { label: "verify", cls: "text-amber-300" },
  delivery: { label: "deliver", cls: "text-emerald-300" },
  system: { label: "system", cls: "text-slate-500" },
};

const TERMINAL = new Set(["COMPLETE", "FAILED", "REJECTED"]);

type StageState = "pending" | "active" | "done" | "halted" | "failed" | "skipped";

interface LogLine {
  key: string;
  seq: number | null;
  text: string;
  stage: StageId | "system";
}

export default function ScanProgress({ jobId }: ScanProgressProps) {
  const router = useRouter();
  const [status, setStatus] = useState<JobProgress | null>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [streamState, setStreamState] = useState<"connecting" | "open" | "reconnecting" | "closed">("connecting");
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [planEquivalenceResults, setPlanEquivalenceResults] = useState<PlanEquivalenceResult | null>(null);
  const [approvalDecision, setApprovalDecision] = useState<ApprovalDecision | null>(null);
  const [approvalRequest, setApprovalRequest] = useState<ApprovalRequest | null>(null);
  const [runSummary, setRunSummary] = useState<string | null>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const stickToBottom = useRef(true);

  const jobStatus = status?.status ?? "RUNNING";
  const isTerminal = TERMINAL.has(jobStatus);

  // /status doesn't carry pending_approval/plan_equivalence_results (that's
  // in /results) - fetched when the job halts. Returns whether it actually
  // succeeded, so the caller only marks this "done" on a real success.
  const fetchApprovalDetails = useCallback(async (): Promise<boolean> => {
    try {
      const results = await fetchJobResults(jobId);
      setPendingApproval(results.pending_approval ?? null);
      setPlanEquivalenceResults(results.plan_equivalence_results ?? null);
      setApprovalDecision(results.approval_decision ?? null);
      setApprovalRequest(results.approval_request ?? null);
      setRunSummary(runSummaryLine(results));
      return true;
    } catch {
      return false;
    }
  }, [jobId]);

  // Status polling - the source of truth for stage progress.
  useEffect(() => {
    let cancelled = false;
    let approvalDetailsFetched = false;
    let interval: ReturnType<typeof setInterval> | null = null;

    const check = async () => {
      try {
        const next = await fetchJobStatus(jobId);
        if (cancelled) return;
        setStatus((prev) => ({
          ...next,
          // Never let a stale poll move the bar backwards.
          progress_percentage: Math.max(prev?.progress_percentage ?? 0, next.progress_percentage ?? 0),
        }));
        if ((next.status === "AWAITING_APPROVAL" || next.status === "REJECTED") && !approvalDetailsFetched) {
          fetchApprovalDetails().then((ok) => {
            if (ok) approvalDetailsFetched = true;
          });
        }
        if (TERMINAL.has(next.status) && interval) {
          clearInterval(interval);
          interval = null;
          if (next.status === "COMPLETE") fetchApprovalDetails();
        }
      } catch {
        // Retry on next interval
      }
    };

    check();
    interval = setInterval(check, 1000);
    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
    };
  }, [jobId, fetchApprovalDetails]);

  // Live log stream. The server replays the job's history on every connect,
  // so a late or re-connecting client sees every line; "seq" de-duplicates.
  // EventSource reconnects on its own after an error - don't close it then.
  useEffect(() => {
    const seen = new Set<number>();
    const apiBase = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000/api";
    const logUrl = `${apiBase.replace(/\/$/, "")}/scan/${jobId}/logs`;
    const es = new EventSource(logUrl);
    eventSourceRef.current = es;

    es.onopen = () => setStreamState("open");
    es.onerror = () => setStreamState(es.readyState === EventSource.CLOSED ? "closed" : "reconnecting");
    es.onmessage = (event) => {
      let text = event.data as string;
      let agent = "system";
      let seq: number | null = null;
      try {
        const data = JSON.parse(event.data);
        text = data.message ?? event.data;
        agent = data.agent ?? "system";
        seq = typeof data.seq === "number" ? data.seq : null;
      } catch {
        // plain-text line
      }
      if (seq !== null) {
        if (seen.has(seq)) return;
        seen.add(seq);
      }
      const tag = text.match(/^\[AGENT:([a-zA-Z_]+)\]\s*/);
      const stage = STEP_TO_STAGE[tag?.[1] ?? agent] ?? "system";
      const clean = tag ? text.slice(tag[0].length) : text;
      setLogs((prev) => {
        const next = [...prev, { key: seq !== null ? `s${seq}` : `r${prev.length}-${Date.now()}`, seq, text: clean, stage }];
        return seq !== null ? next.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)) : next;
      });
    };

    return () => {
      es.close();
      eventSourceRef.current = null;
    };
  }, [jobId]);

  // Stop the stream a few seconds after the job finishes (the final lines are
  // published just after the status flips).
  useEffect(() => {
    if (!isTerminal) return;
    const t = setTimeout(() => {
      eventSourceRef.current?.close();
      setStreamState("closed");
    }, 4000);
    return () => clearTimeout(t);
  }, [isTerminal]);

  // Auto-scroll unless the user scrolled up to read.
  useEffect(() => {
    const el = logContainerRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [logs]);

  const onLogScroll = () => {
    const el = logContainerRef.current;
    if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  // ---------------------------------------------------------------------
  // Derived stage view
  // ---------------------------------------------------------------------
  const completedStages = status?.completed_stages ?? [];
  const currentStage = status?.current_stage ?? (isTerminal ? null : "infrastructure");
  const summaries = status?.stage_summaries ?? {};
  const iterations = status?.verification_iterations ?? [];
  const maxRepairs = status?.max_repair_iterations ?? 2;
  const repairAttempts = status?.repair_attempts ?? 0;
  const repairs = status?.repair_history ?? [];
  const verdict = status?.verification_verdict ?? null;
  const failed = jobStatus === "FAILED";
  const awaiting = jobStatus === "AWAITING_APPROVAL" || currentStage === "awaiting_approval";
  const rejected = jobStatus === "REJECTED";
  const complete = jobStatus === "COMPLETE";
  const currentStep = status?.current_agent ?? null;

  // The stage that was running when the job stopped (failed or halted).
  const lastActive: StageId | null =
    currentStage && currentStage !== "awaiting_approval" && currentStage !== "complete"
      ? (currentStage as StageId)
      : null;
  // The risk gate lives in the Delivery & Approval Agent.
  const haltedStage: StageId | null = awaiting || rejected ? "delivery" : null;

  const stageState = (id: StageId): StageState => {
    if (failed && lastActive === id) return "failed";
    if ((awaiting || rejected) && haltedStage === id) return "halted";
    if (!failed && !awaiting && !rejected && lastActive === id) return "active";
    if (completedStages.includes(id)) return "done";
    return "pending";
  };

  const loopActive =
    currentStage === "verification" || (currentStage === "iac_engineering" && iterations.length > 0);
  // Count the pass that's running now, not just the finished ones.
  const passNo = iterations.length + (currentStage === "verification" && !isTerminal ? 1 : 0);
  const verified = verdict === "PASS" || verdict === "NEEDS_APPROVAL";
  const doneAgents = STAGES.filter((s) => stageState(s.id) === "done" || stageState(s.id) === "skipped").length;
  const progress = Math.min(100, Math.max(0, status?.progress_percentage ?? 0));

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
      {(awaiting || rejected || (complete && approvalDecision)) && (pendingApproval || approvalRequest) && (
        <div className="lg:col-span-12">
          <PendingApprovalPanel
            jobId={jobId}
            pendingApproval={pendingApproval}
            approvalRequest={approvalRequest}
            planEquivalenceResults={planEquivalenceResults}
            approvalDecision={approvalDecision}
            mode={jobStatus === "AWAITING_APPROVAL" ? "actionable" : "readonly"}
            onDecision={() => {
              fetchApprovalDetails();
            }}
          />
        </div>
      )}

      {/* Outcome banners */}
      {complete && (
        <OutcomeBanner
          tone={verified ? "emerald" : "amber"}
          icon={verified ? CheckCircle2 : AlertTriangle}
          title={verified ? "Pipeline complete" : "Delivered, but not fully verified"}
          body={
            !verified
              ? verdict === "FAIL"
                ? `Validation still failed after ${repairAttempts} repair cycle${repairAttempts === 1 ? "" : "s"}. Review the bundle before using it.`
                : `Some checks couldn't run: ${(iterations[iterations.length - 1]?.incomplete_reasons ?? []).join("; ") || "see the log"}.`
              : iterations.length > 1
              ? `Verified after ${iterations.length} passes and ${repairAttempts} repair cycle${repairAttempts === 1 ? "" : "s"}. The bundle and reports are ready.`
              : "Verified on the first pass. The bundle and reports are ready."
          }
          summary={runSummary}
          action={
            <button
              onClick={() => router.push(`/results/${jobId}#deliverables`)}
              className={`btn text-white shadow-sm shrink-0 ${verified ? "bg-emerald-600 hover:bg-emerald-700" : "bg-amber-600 hover:bg-amber-700"}`}
            >
              View results
              <ArrowRight className="w-4 h-4" />
            </button>
          }
        />
      )}
      {failed && (
        <OutcomeBanner
          tone="rose"
          icon={AlertTriangle}
          title={`Pipeline failed${lastActive ? ` in the ${STAGES.find((s) => s.id === lastActive)?.name ?? lastActive}` : ""}`}
          body={status?.error || "The pipeline failed. See the log for details."}
          action={
            <button onClick={() => router.push("/scan")} className="btn-secondary shrink-0">
              Start a new scan
              <ArrowRight className="w-3.5 h-3.5" />
            </button>
          }
        />
      )}
      {rejected && (
        <OutcomeBanner
          tone="slate"
          icon={XCircle}
          title="Rejected, pipeline stopped"
          body="A human rejected the pending findings above. This job will not produce an adoptable bundle."
          action={
            <button onClick={() => router.push("/scan")} className="btn-secondary shrink-0">
              Start a new scan
              <ArrowRight className="w-3.5 h-3.5" />
            </button>
          }
        />
      )}

      {/* Agents */}
      <div className="lg:col-span-7 space-y-4">
        <div className="card p-4 sm:p-5">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-slate-700">
              {doneAgents}/{STAGES.length} agents finished
            </span>
            <span className="text-xs font-bold text-slate-900 tabular-nums">{Math.round(progress)}%</span>
          </div>
          <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ease-out ${
                failed ? "bg-rose-500" : complete ? "bg-emerald-500" : awaiting ? "bg-amber-500" : "bg-brand-600"
              }`}
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>

        <StageCard
          def={STAGES[0]}
          state={stageState("infrastructure")}
          summary={summaries.infrastructure}
          currentStep={currentStep}
          index={1}
        />

        {/* The self-correcting loop: IaC Engineering fixes, Verification judges */}
        <div
          className={`rounded-2xl border-2 border-dashed p-3 sm:p-4 space-y-3 transition-colors ${
            loopActive ? "border-brand-300 bg-brand-50/30" : "border-slate-200"
          }`}
        >
          <div className="flex items-center justify-between gap-3 px-1">
            <div className="flex items-center gap-2 text-xs font-bold text-slate-700">
              <RefreshCw className={`w-3.5 h-3.5 text-brand-600 ${loopActive && !awaiting ? "animate-spin [animation-duration:3s]" : ""}`} />
              Generate ⇄ verify loop
            </div>
            <span className="text-2xs font-semibold text-slate-500 tabular-nums">
              {passNo === 0
                ? `up to ${maxRepairs} repair cycles`
                : `Pass ${passNo}/${maxRepairs + 1} · repairs ${repairAttempts}/${maxRepairs}`}
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <StageCard
              def={STAGES[1]}
              state={stageState("iac_engineering")}
              summary={summaries.iac_engineering}
              currentStep={currentStep}
              index={2}
              compact
            />
            <StageCard
              def={STAGES[2]}
              state={stageState("verification")}
              summary={summaries.verification}
              currentStep={currentStep}
              index={3}
              compact
            />
          </div>

          {iterations.length > 0 && (
            <IterationTimeline iterations={iterations} repairs={repairs} maxRepairs={maxRepairs} />
          )}
        </div>

        <StageCard
          def={STAGES[3]}
          state={stageState("delivery")}
          summary={summaries.delivery}
          currentStep={currentStep}
          index={4}
        />
      </div>

      {/* Live log */}
      <div className="lg:col-span-5 lg:sticky lg:top-8 flex flex-col h-[520px] lg:h-[calc(100vh-8rem)] lg:max-h-[760px] rounded-2xl border border-slate-800 bg-[#090d16] shadow-xl overflow-hidden">
        <div className="px-4 py-3 bg-[#0f1422] border-b border-slate-800 flex items-center justify-between">
          <div className="flex items-center gap-2 text-xs font-mono text-slate-300 font-medium">
            <Terminal className="w-3.5 h-3.5 text-brand-400" />
            <span>Agent log</span>
            <span className="text-slate-600">·</span>
            <span className="text-slate-500 tabular-nums">{logs.length} lines</span>
          </div>
          <StreamBadge jobStatus={jobStatus} stream={streamState} />
        </div>

        <div
          ref={logContainerRef}
          onScroll={onLogScroll}
          className="flex-1 py-3 font-mono overflow-y-auto leading-relaxed selection:bg-brand-600 selection:text-white"
        >
          {logs.length === 0 ? (
            <div className="px-4 text-slate-500 italic py-2 flex items-center gap-2 text-xs">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              {streamState === "open" ? "Connected. Waiting for the first log line..." : "Connecting to the log stream..."}
            </div>
          ) : (
            logs.map((l, i) => {
              const tag = STAGE_TAG[l.stage];
              const isError = /error|failed|✗/i.test(l.text) && !/0 failed/i.test(l.text);
              const isGood = /✓|passed\.|complete|Bundle ready/i.test(l.text);
              return (
                <div key={l.key} className="flex items-start gap-2.5 px-4 py-px hover:bg-white/[0.03] text-2xs">
                  <span className="text-slate-600 select-none tabular-nums w-7 text-right shrink-0">{i + 1}</span>
                  <span className={`shrink-0 w-[4.5rem] ${tag.cls}`}>{tag.label}</span>
                  <span
                    className={`break-words min-w-0 ${
                      isError ? "text-rose-400" : isGood ? "text-emerald-400" : "text-slate-300"
                    }`}
                  >
                    {l.text}
                  </span>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

// The end-of-run line: "N found: X managed, Y referenced, Z excluded ·
// Migration Safety N% · nothing changed in AWS · K findings ...". Every number
// comes from the results; anything that wasn't measured says so instead of
// showing a number. "Nothing changed in AWS" is about TerraAgent itself (it only
// ever reads) - whether adopting would change anything is Migration Safety's job.
function runSummaryLine(r: JobResults): string {
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  const parts: string[] = [];

  const s = r.infra_model?.summary;
  if (s) {
    const split = [`${s.manage ?? 0} managed`, `${s.reference ?? 0} referenced`, `${s.exclude ?? 0} excluded`];
    if (s.review) split.push(`${s.review} in review`);
    parts.push(`${s.total} found: ${split.join(", ")}`);
  } else {
    parts.push(`${r.resources_count ?? 0} found`);
  }

  const ms = r.migration_safety;
  parts.push(ms && ms.score !== null ? `Migration Safety ${ms.score}%` : "Migration Safety not measured");
  parts.push("nothing changed in AWS");

  const sp = r.security_posture;
  // A rejected Hardening proposal ships no files, so its changes don't count.
  const fixes = r.hardening?.files && Object.keys(r.hardening.files).length > 0 ? r.hardening.changes?.length ?? 0 : 0;
  if (!sp || sp.score === null) {
    parts.push("security findings not measured");
  } else if (fixes > 0) {
    parts.push(`${plural(sp.total_findings, "finding", "findings")}, ${plural(fixes, "fix", "fixes")} in a separate Hardening PR`);
  } else {
    parts.push(`${plural(sp.total_findings, "security finding", "security findings")} reported, no Hardening fixes`);
  }
  return parts.join(" · ");
}

function OutcomeBanner({
  tone,
  icon: Icon,
  title,
  body,
  summary,
  action,
}: {
  tone: "emerald" | "amber" | "rose" | "slate";
  icon: LucideIcon;
  title: string;
  body: string;
  summary?: string | null;
  action: React.ReactNode;
}) {
  const styles = {
    emerald: { wrap: "border-emerald-200 bg-emerald-50/60", icon: "bg-emerald-600", title: "text-emerald-900", body: "text-emerald-800/80" },
    amber: { wrap: "border-amber-200 bg-amber-50/60", icon: "bg-amber-600", title: "text-amber-900", body: "text-amber-800/90" },
    rose: { wrap: "border-rose-200 bg-rose-50/60", icon: "bg-rose-600", title: "text-rose-900", body: "text-rose-800/90" },
    slate: { wrap: "bg-slate-50", icon: "bg-slate-500", title: "text-slate-900", body: "text-slate-600" },
  }[tone];
  return (
    <div
      className={`lg:col-span-12 card p-4 sm:p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-4 animate-fade-in ${styles.wrap}`}
    >
      <div className="flex items-start gap-3 min-w-0">
        <div className={`p-2 rounded-xl text-white shrink-0 ${styles.icon}`}>
          <Icon className="w-5 h-5" />
        </div>
        <div className="min-w-0">
          <div className={`text-sm font-bold ${styles.title}`}>{title}</div>
          <p className={`text-xs leading-relaxed break-words ${styles.body}`}>{body}</p>
          {summary && <p className={`text-xs font-semibold mt-1.5 break-words ${styles.title}`}>{summary}</p>}
        </div>
      </div>
      {action}
    </div>
  );
}

function StageCard({
  def,
  state,
  summary,
  currentStep,
  index,
  compact = false,
}: {
  def: StageDef;
  state: StageState;
  summary?: string;
  currentStep: string | null;
  index: number;
  compact?: boolean;
}) {
  const Icon = def.icon;
  const activeStep = state === "active" ? def.steps.find((s) => s.id === currentStep) : undefined;

  const badge = {
    pending: { text: "Waiting", cls: "bg-slate-100 text-slate-500" },
    active: { text: "Running", cls: "bg-brand-600 text-white" },
    done: { text: "Done", cls: "bg-emerald-50 text-emerald-700" },
    halted: { text: "Needs approval", cls: "bg-amber-100 text-amber-800" },
    failed: { text: "Failed", cls: "bg-rose-100 text-rose-700" },
    skipped: { text: "Skipped", cls: "bg-slate-100 text-slate-500" },
  }[state];

  const iconCls = {
    pending: "bg-slate-100 text-slate-400",
    active: "bg-brand-600 text-white",
    done: "bg-emerald-50 text-emerald-600",
    halted: "bg-amber-50 text-amber-600",
    failed: "bg-rose-50 text-rose-600",
    skipped: "bg-slate-100 text-slate-400",
  }[state];

  return (
    <div
      className={`card p-4 transition-all ${
        state === "active"
          ? "ring-2 ring-brand-500/25 border-brand-300"
          : state === "failed"
          ? "border-rose-200"
          : state === "halted"
          ? "border-amber-300"
          : state === "pending"
          ? "opacity-70"
          : ""
      }`}
    >
      <div className="flex items-start gap-3">
        <div className={`p-2 rounded-xl shrink-0 ${iconCls}`}>
          {state === "active" ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : state === "done" ? (
            <CheckCircle2 className="w-4 h-4" />
          ) : state === "failed" ? (
            <XCircle className="w-4 h-4" />
          ) : state === "halted" ? (
            <PauseCircle className="w-4 h-4" />
          ) : (
            <Icon className="w-4 h-4" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="text-sm font-bold text-slate-900 flex items-center gap-1.5 flex-wrap">
                <span className="text-slate-400 font-semibold tabular-nums">{index}.</span>
                {def.name}
                {def.usesLlm && (
                  <span className="text-3xs font-semibold px-1.5 py-px rounded bg-violet-50 text-violet-700 border border-violet-100">
                    LLM
                  </span>
                )}
              </div>
              {!compact && <p className="text-2xs text-slate-500 mt-0.5">{def.desc}</p>}
            </div>
            <span className={`text-3xs font-bold uppercase tracking-wider px-2 py-0.5 rounded-md shrink-0 ${badge.cls}`}>
              {badge.text}
            </span>
          </div>

          {summary && state !== "active" && (
            <p className={`text-xs mt-2 font-medium ${state === "failed" ? "text-rose-700" : "text-slate-700"}`}>{summary}</p>
          )}
          {state === "active" && (
            <p className="text-xs mt-2 text-brand-800 font-medium">{activeStep ? `${activeStep.label}…` : "Starting…"}</p>
          )}

          {!compact && (
            <div className="flex flex-wrap gap-1.5 mt-2.5">
              {def.steps.map((s) => (
                <span
                  key={s.id}
                  className={`text-3xs px-2 py-0.5 rounded-md border ${
                    activeStep?.id === s.id
                      ? "border-brand-300 bg-brand-50 text-brand-800 font-semibold"
                      : "border-slate-200 text-slate-500"
                  }`}
                >
                  {s.label}
                </span>
              ))}
            </div>
          )}
          {compact && (
            <p className="text-3xs text-slate-400 mt-1.5 leading-relaxed">{def.steps.map((s) => s.label).join(" · ")}</p>
          )}
        </div>
      </div>
    </div>
  );
}

const VERDICT_STYLE: Record<string, { text: string; cls: string; icon: LucideIcon }> = {
  PASS: { text: "Verified", cls: "text-emerald-700", icon: CheckCircle2 },
  FAIL: { text: "Back to repair", cls: "text-rose-700", icon: Wrench },
  INCOMPLETE: { text: "Incomplete", cls: "text-amber-700", icon: AlertTriangle },
  NEEDS_APPROVAL: { text: "Needs approval", cls: "text-amber-700", icon: PauseCircle },
};

function IterationTimeline({
  iterations,
  repairs,
  maxRepairs,
}: {
  iterations: VerificationIteration[];
  repairs: RepairEntry[];
  maxRepairs: number;
}) {
  return (
    <ol className="rounded-xl bg-white border border-slate-200 divide-y divide-slate-100">
      {iterations.map((it, i) => {
        const last = i === iterations.length - 1;
        const style =
          it.verdict === "FAIL" && last && i >= maxRepairs
            ? { text: "Still failing", cls: "text-rose-700", icon: XCircle }
            : VERDICT_STYLE[it.verdict] ?? VERDICT_STYLE.INCOMPLETE;
        const OutcomeIcon = style.icon;
        const repair = repairs[i];
        return (
          <li key={it.iteration} className="text-2xs">
            <div className="px-3 py-2 flex items-center gap-3">
              <span className="font-bold text-slate-900 tabular-nums w-12 shrink-0">Pass {it.iteration}</span>
              <div className="flex-1 min-w-0 flex flex-wrap gap-x-3 gap-y-0.5 text-slate-600">
                <Check ok={it.validation_passed} label={it.system_failure ? "validate (env)" : "validate"} />
                {(it.checks_run ?? []).includes("policy") ? (
                  <>
                    <span>
                      {it.plan_changes === null ? "plan not run" : `plan: ${it.plan_changes} change${it.plan_changes === 1 ? "" : "s"}`}
                    </span>
                    {it.imported != null && it.imported > 0 && <span>{it.imported} imported</span>}
                    {it.config_mismatches != null && (
                      <span className={it.config_mismatches > 0 ? "text-amber-700" : "text-emerald-700"}>
                        {it.config_mismatches > 0
                          ? `${it.config_mismatches} attribute mismatch${it.config_mismatches === 1 ? "" : "es"} vs live`
                          : "attributes match live"}
                      </span>
                    )}
                    {it.drift_findings > 0 && <span className="text-amber-700">{it.drift_findings} drift</span>}
                    <span className={it.high_findings > 0 ? "text-amber-700" : ""}>
                      {it.high_findings} high/critical reported
                    </span>
                  </>
                ) : (
                  <span className="text-slate-400">later checks wait for validate</span>
                )}
              </div>
              <span className={`flex items-center gap-1 font-semibold shrink-0 ${style.cls}`}>
                <OutcomeIcon className="w-3 h-3" />
                {style.text}
              </span>
            </div>
            {(it.incomplete_reasons ?? []).length > 0 && (
              <div className="px-3 pb-2 -mt-1 text-amber-800">Not verified: {it.incomplete_reasons.join("; ")}</div>
            )}
            {repair && (
              <div className="mx-3 mb-2 px-2.5 py-1.5 rounded-lg bg-violet-50 border border-violet-100 text-violet-900 flex flex-wrap gap-x-3 gap-y-0.5">
                <span className="font-semibold">Repair {repair.cycle}</span>
                <span>
                  fixed {repair.fixed.length}
                  {repair.fixed.length ? `: ${repair.fixed.slice(0, 2).join(", ")}` : ""}
                </span>
                {repair.rejected.length > 0 && (
                  <span
                    className="text-rose-700"
                    title={repair.rejected.map((r) => `${r.address}: ${r.violations.join(", ")}`).join("; ")}
                  >
                    {repair.rejected.length} rejected by invariant checks
                  </span>
                )}
                {repair.unresolved.length > 0 && <span className="text-slate-500">{repair.unresolved.length} unresolved</span>}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Check({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className={`inline-flex items-center gap-1 ${ok ? "text-emerald-700" : "text-rose-700"}`}>
      {ok ? <CheckCircle2 className="w-3 h-3" /> : <Circle className="w-3 h-3" />}
      {label}
    </span>
  );
}

function StreamBadge({
  jobStatus,
  stream,
}: {
  jobStatus: string;
  stream: "connecting" | "open" | "reconnecting" | "closed";
}) {
  const s =
    jobStatus === "FAILED"
      ? { label: "FAILED", dot: "bg-rose-500", text: "text-rose-400", ping: false }
      : jobStatus === "COMPLETE"
      ? { label: "DONE", dot: "bg-emerald-500", text: "text-emerald-400", ping: false }
      : jobStatus === "REJECTED"
      ? { label: "HALTED", dot: "bg-slate-400", text: "text-slate-400", ping: false }
      : jobStatus === "AWAITING_APPROVAL"
      ? { label: "PAUSED", dot: "bg-amber-500", text: "text-amber-400", ping: false }
      : stream === "open"
      ? { label: "LIVE", dot: "bg-emerald-500", text: "text-emerald-400", ping: true }
      : stream === "reconnecting"
      ? { label: "RECONNECTING", dot: "bg-amber-500", text: "text-amber-400", ping: true }
      : { label: "CONNECTING", dot: "bg-slate-500", text: "text-slate-400", ping: true };
  return (
    <div className="flex items-center gap-2">
      <span className="relative flex h-2 w-2">
        {s.ping && <span className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${s.dot}`} />}
        <span className={`relative inline-flex rounded-full h-2 w-2 ${s.dot}`} />
      </span>
      <span className={`text-3xs font-mono font-bold tracking-wider ${s.text}`}>{s.label}</span>
    </div>
  );
}
