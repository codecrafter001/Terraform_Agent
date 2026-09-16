"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import PendingApprovalPanel from "./PendingApprovalPanel";
import { ApprovalDecision, JobStatus, PendingApproval, PlanEquivalenceResult } from "@/lib/types";

interface ResultsApprovalSectionProps {
  jobId: string;
  status: JobStatus;
  pendingApproval?: PendingApproval | null;
  planEquivalenceResults?: PlanEquivalenceResult | null;
  approvalDecision?: ApprovalDecision | null;
}

export default function ResultsApprovalSection({
  jobId,
  status,
  pendingApproval,
  planEquivalenceResults,
  approvalDecision,
}: ResultsApprovalSectionProps) {
  const router = useRouter();
  const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (refreshTimer.current) clearInterval(refreshTimer.current);
    };
  }, []);

  if (!pendingApproval || (pendingApproval.findings || []).length === 0) return null;

  const isActionable = status === "AWAITING_APPROVAL";

  const handleDecision = () => {
    // This page is server-rendered - the approve/reject POST already
    // happened by the time this fires. cost_agent + documentation_agent run
    // as a background tail (a few seconds), so re-fetch this server
    // component a handful of times rather than once immediately, which
    // would likely just show "RUNNING" again.
    let attempts = 0;
    refreshTimer.current = setInterval(() => {
      attempts += 1;
      router.refresh();
      if (attempts >= 8 && refreshTimer.current) {
        clearInterval(refreshTimer.current);
      }
    }, 3000);
  };

  return (
    <PendingApprovalPanel
      jobId={jobId}
      pendingApproval={pendingApproval}
      planEquivalenceResults={planEquivalenceResults}
      approvalDecision={approvalDecision}
      mode={isActionable ? "actionable" : "readonly"}
      onDecision={handleDecision}
    />
  );
}
