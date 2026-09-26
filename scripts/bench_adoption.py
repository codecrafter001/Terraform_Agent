#!/usr/bin/env python3
"""Adoption test bench - runs one real scan against the sandbox account through
the TerraAgent API and reports the success metrics from the plan:

  - no-op rate            share of managed resources the plan leaves unchanged (target > 90%)
  - destroy / replace     must be 0 in any adoption
  - classification        accuracy against a hand-labelled answer key (optional)
  - human-review rate     share of discovered resources left in Review
  - scan time             end to end

Read-only like everything else: the scan runs `terraform plan`, never apply.
If the run pauses at the risk gate, the bench reports that and stops - it
never approves anything on its own.

Usage (credentials from the environment, never from arguments):

  export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... [AWS_SESSION_TOKEN=...]
  export TERRAAGENT_ROLE_ARN=... TERRAAGENT_EXTERNAL_ID=...     # optional, recommended
  export TERRAAGENT_API_KEY=...                                  # if the API requires one
  python scripts/bench_adoption.py --region us-east-1 --answer-key bench/answer_key.json \\
      --out bench/results/$(date +%F).json

Answer key format: {"<resource id>": "manage" | "reference" | "exclude" | "review", ...}
Exit code: 0 if every target is met, 1 if not, 2 if the run didn't finish.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

NO_OP_TARGET = 90.0


def _request(api: str, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if os.getenv("TERRAAGENT_API_KEY"):
        headers["X-API-Key"] = os.environ["TERRAAGENT_API_KEY"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{api}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def run(api: str, region: str, filters: list, timeout_s: int) -> Dict[str, Any]:
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not os.getenv(var):
            sys.exit(f"{var} is not set")
    body = {
        "aws_access_key": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_session_token": os.getenv("AWS_SESSION_TOKEN"),
        "role_arn": os.getenv("TERRAAGENT_ROLE_ARN"),
        "external_id": os.getenv("TERRAAGENT_EXTERNAL_ID"),
        "region": region,
        "operation": "generate",
        "resource_filters": filters,
        "run_plan_equivalence": True,
    }
    started = time.monotonic()
    job_id = _request(api, "POST", "/scan", body)["job_id"]
    del body  # the credentials are not needed past this point
    print(f"job {job_id} started", file=sys.stderr)
    status: Dict[str, Any] = {}
    while time.monotonic() - started < timeout_s:
        status = _request(api, "GET", f"/scan/{job_id}/status")
        if status.get("status") in ("COMPLETE", "FAILED", "REJECTED", "AWAITING_APPROVAL"):
            break
        time.sleep(10)
    results = _request(api, "GET", f"/scan/{job_id}/results")
    return {"job_id": job_id, "seconds": round(time.monotonic() - started), "status": status, "results": results}


def metrics(run_out: Dict[str, Any], answer_key: Optional[Dict[str, str]]) -> Dict[str, Any]:
    results, status = run_out["results"], run_out["status"]
    safety = results.get("migration_safety") or {}
    model = results.get("infra_model") or {}
    records = model.get("records") or []
    total = len(records)
    review = sum(1 for r in records if r.get("decision") == "review")
    managed = safety.get("resources_managed") or 0

    out: Dict[str, Any] = {
        "job_id": run_out["job_id"],
        "final_status": status.get("status"),
        "verdict": status.get("verification_verdict"),
        "scan_seconds": run_out["seconds"],
        "resources_discovered": total,
        "resources_managed": managed,
        "no_op_rate": safety.get("score") if safety.get("basis") == "plan" else None,
        "no_op_basis": safety.get("basis"),
        "destroy_or_replace": safety.get("destroy_or_replace"),
        "changing_resources": safety.get("changing_resources") or [],
        "human_review_rate": round(100 * review / total, 1) if total else None,
        "security_posture": (results.get("security_posture") or {}).get("score"),
        "incomplete_reasons": ((status.get("verification_iterations") or [{}])[-1]).get("incomplete_reasons", []),
    }
    if answer_key:
        by_id = {r.get("id"): r.get("decision") for r in records}
        scored = {rid: (expected, by_id.get(rid)) for rid, expected in answer_key.items()}
        correct = sum(1 for expected, got in scored.values() if expected == got)
        out["classification_accuracy"] = round(100 * correct / len(scored), 1) if scored else None
        out["classification_mismatches"] = {rid: {"expected": e, "got": g} for rid, (e, g) in scored.items() if e != g}
        out["not_in_answer_key"] = sorted(set(by_id) - set(answer_key))
    out["targets_met"] = (
        out["final_status"] == "COMPLETE"
        and out["no_op_rate"] is not None and out["no_op_rate"] >= NO_OP_TARGET
        and out["destroy_or_replace"] == 0
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.getenv("TERRAAGENT_API", "http://localhost/api"))
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--filters", default="VPC,SG,EC2,S3,RDS,IAM")
    parser.add_argument("--answer-key")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--out")
    args = parser.parse_args()

    answer_key = None
    if args.answer_key:
        with open(args.answer_key, encoding="utf-8") as f:
            answer_key = json.load(f)
    try:
        run_out = run(args.api, args.region, args.filters.split(","), args.timeout)
    except urllib.error.URLError as e:
        print(f"API unreachable: {e}", file=sys.stderr)
        return 2
    report = metrics(run_out, answer_key)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    if report["final_status"] not in ("COMPLETE",):
        return 2
    return 0 if report["targets_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
