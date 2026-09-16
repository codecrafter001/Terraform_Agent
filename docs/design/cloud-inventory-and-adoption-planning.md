# Design: Cloud Inventory & Ownership, Dependency Graph, Adoption Planning

Deep-dive design for three sections of the Phase 1 roadmap (`C:\Users\USER\.claude\plans\playful-stargazing-quail.md`, Increments 1-2): cloud inventory/ownership, dependency graph upgrades, and the Adoption Planning Agent. Grounded against the actual current implementation, not aspirational.

---

## Part A — Cloud Inventory & Ownership

### Current state (exact)

`AWSScanner` (`backend/tools/aws_scanner.py`) discovers exactly 8 resource types: `aws_vpc`, `aws_subnet`, `aws_route_table`, `aws_security_group`, `aws_instance`, `aws_s3_bucket`, `aws_db_instance`, `aws_iam_role`. Single region per scan (`region` param), single account per scan (optionally via `role_arn` STS AssumeRole — no Organizations fan-out). Each resource dict carries `resource_type`, `id`, `name`, `tags` (AWS-style `[{"Key":...,"Value":...}]` list), plus type-specific fields. No ownership inference, no creation-time metadata beyond what `describe_instances`' `LaunchTime` already returns implicitly (unused today), no cost data, no CloudTrail, no AWS Config.

### Target: normalized inventory record

Every resource classification/adoption/graph agent downstream should read a single normalized shape rather than re-deriving ownership/age/region logic per-agent. Extend the resource dict (not replace it — additive, so existing composer/graph code keeps working unchanged) with an `inventory` sub-object populated by a new deterministic post-processing step at the end of `cloud_discovery_node`, before the resource list is returned:

```python
{
    "resource_type": "aws_instance",
    "id": "i-0123...",
    "name": "...",
    "tags": [...],           # unchanged, existing field
    # ... existing type-specific fields unchanged ...
    "inventory": {
        "region": "us-east-1",
        "account_id": "123456789012",      # from sts:GetCallerIdentity, cached once per scan
        "owner": "platform-team",           # inferred, see below
        "environment": "production",        # inferred, see below
        "created_at": "2024-03-01T12:00:00Z",  # where the API provides it, else null
        "age_days": 583,                    # derived, null if created_at unknown
        "tag_completeness": 0.6             # fraction of a configurable required-tags set present
    }
}
```

### Ownership/environment inference — deterministic, tag-first

No CloudTrail integration in Phase 1 (correctly deferred — it's a new read-only AWS API surface unrelated to the 8 items above). Ownership inference is therefore tag-based only, which is honest about its limits but still genuinely useful:

```python
OWNER_TAG_KEYS = ["Owner", "owner", "Team", "team", "CostCenter", "cost-center"]
ENV_TAG_KEYS = ["Environment", "environment", "Env", "env", "Stage", "stage"]

def infer_owner(tags: List[dict]) -> Optional[str]:
    tag_map = {t["Key"]: t["Value"] for t in tags}
    for key in OWNER_TAG_KEYS:
        if key in tag_map:
            return tag_map[key]
    return None  # explicitly None, not "unknown" - let the frontend render "no owner tag" honestly
```
Same pattern for environment. This is intentionally dumb and predictable — a resource either has a recognizable ownership tag or it doesn't; no LLM guessing about ownership, since a wrong ownership *guess* is worse than an honest "unknown" (a wrong guess could misdirect a real migration decision).

### Creation-time metadata — per-API-call reality check

This is not uniformly available and the plan should say so precisely rather than promise "resource creation time" as a blanket feature:
- `aws_instance`: `describe_instances` already returns `LaunchTime` — free, just needs to be read into `inventory.created_at` (`aws_scanner.py::scan_ec2_instances`, one new field read, zero new API calls).
- `aws_db_instance`: `describe_db_instances` returns `InstanceCreateTime` — same, free.
- `aws_s3_bucket`: `list_buckets` returns `CreationDate` — already partially read into the resource dict today (`b.get("CreationDate")`), just needs mirroring into `inventory.created_at`.
- `aws_vpc`, `aws_subnet`, `aws_route_table`, `aws_security_group`, `aws_iam_role`: **no creation timestamp in the Describe/List response at all.** `aws_iam_role`'s `describe_role`/`list_roles` does return `CreateDate` actually — worth double-checking and wiring if so. VPC/subnet/route-table/security-group genuinely have no creation-time field in their Describe APIs; getting it would require CloudTrail (`CreateVpc` event lookup) — explicitly out of scope. Don't fabricate an age for these; leave `created_at: null`.

### Cross-region scanning — concrete mechanism

`AWSScanner.__init__` takes a single `region: str`. Add a new orchestration layer *above* `AWSScanner`, not inside it (keep the scanner single-region and simple):

```python
# backend/tools/multi_region_scanner.py (new)
async def scan_all_regions(access_key, secret_key, session_token, regions: List[str], filters, endpoint_url=None) -> List[dict]:
    # regions default: call ec2.describe_regions() once with the first region's client,
    # or accept an explicit list from the caller (safer default: explicit list, since
    # scanning all ~30 AWS regions by default would be slow and mostly empty for most accounts)
    all_resources = []
    for region in regions:
        scanner = AWSScanner(access_key, secret_key, region=region, session_token=session_token, endpoint_url=endpoint_url)
        resources = scanner.scan_all(filters=filters)
        for r in resources:
            r.setdefault("inventory", {})["region"] = region
        all_resources.extend(resources)
    return all_resources
```
Run regions concurrently via `asyncio.gather` wrapping `asyncio.to_thread(scanner.scan_all, filters)` (matches the existing sync-boto3-in-async-context pattern already used for `list_organization_accounts` in `routers/organizations.py`). `ScanRequest.region: str` becomes `ScanRequest.regions: List[str] = ["us-east-1"]` (breaking rename — acceptable pre-1.0, or keep `region` for backward compat and add `regions: Optional[List[str]]` that overrides it when present).

**Sequencing note:** this is genuinely independent of Increments 1/2 in the main plan and can land any time after Increment 0 — it doesn't touch classification/adoption-plan logic, just enriches what `cloud_discovery_node` returns. Recommend building it right after Increment 1 (classification), since classification rules (e.g., "shared/default SG per VPC") get more interesting once there's more than one region's worth of default SGs to classify.

### AWS Organizations / multi-account — extension point only, not built now

Confirmed correctly deferred in the main plan. The concrete extension point, so it's clear this isn't a redesign later: `routers/organizations.py::list_organization_accounts` already exists and returns account IDs. A future fan-out orchestrator would call it once, then call `scan_all_regions` (above) once per account_id using `role_arn` templated per-account (`arn:aws:iam::{account_id}:role/OrganizationAccountAccessRole` or an org-specific naming convention), aggregating into a *list of per-account inventories* rather than trying to force multi-account resources into the current single-`TerraAgentState`-per-job shape. This is a job-orchestration change (N sub-jobs, one LangGraph run each, results aggregated by a parent job), not a `cloud_discovery_node` change — correctly Phase 2 scope.

### Shared-resource / AWS-managed filtering — this is Increment 1's classifier, cross-referenced

Don't duplicate the ruleset here — see the main plan's Increment 1 (`backend/tools/resource_classifier.py`). One addition worth folding into that classifier once cross-region scanning lands: a resource appearing identically in multiple regions with the same name (e.g., an IAM role, which is global but might get *discovered* once per region scan since IAM isn't actually regional — dedupe IAM/Organizations-scope resources by ARN across region loops in `scan_all_regions`, don't let them appear N times for N regions scanned).

---

## Part B — Better Dependency Graph

### Current state (exact)

`DependencyGraphBuilder.build_graph()` (`backend/tools/graph_builder.py`) derives edges *only* from `vpc_id`, `subnet_id`, and `security_groups` reference fields, producing `relation` values `"contains"` | `"hosted_in"` | `"secured_by"`. Output: `{nodes, links, is_dag, node_count, edge_count, topological_order, dot}`. `_categorize_type()` buckets into 6 categories (Database/Networking/Compute/Storage/Security/General). **No IAM edges, no data-plane edges (ALB/ECS/target-group), no stack-boundary concept.**

### The real prerequisite nobody should skip: discovery scope

The user's example stack layout (`security/kms.tf`, `data/rds.tf`, `application/alb.tf`, `application/ecs.tf`) references KMS, ALB, and ECS — **none of which `AWSScanner` discovers today.** A "better dependency graph" is bounded by what's actually in `state["resources"]`; graph logic can't infer relationships to resources that were never scanned. Two options, and this plan recommends (b):

- (a) Expand `AWSScanner` first (add `scan_load_balancers`, `scan_ecs_services`, `scan_kms_keys` following the exact pattern of the 8 existing `scan_*` methods) — real work, its own increment, not implicitly free inside "improve the graph."
- (b) **Build the graph improvements against the 8 resource types that exist now**, and treat "discovery breadth" as a separate, explicitly-tracked backlog item. The IAM-relationship and orphan-detection improvements below are fully buildable today without touching `AWSScanner` at all — do those first, get real value, then decide whether ALB/ECS/KMS discovery earns its own increment based on what a real scan against a real account actually shows is missing.

### IAM policy relationship edges — concretely buildable today

`aws_iam_role` resources already carry `attached_policy_arns` (list of ARNs) and `assume_role_policy` (the trust policy JSON, already returned raw by `scan_iam_roles`). Two edge types, both derivable from data already in state:

1. **Instance → Role** edges: `aws_instance` doesn't currently carry an IAM instance profile reference (`describe_instances` returns `IamInstanceProfile.Arn` — not read into the resource dict today). Add that one field read to `scan_ec2_instances` (cheap, no new API call), then in the graph builder: `if instance.get("iam_instance_profile_arn")`, add an edge `instance -> role` with `relation: "assumes_role"`.
2. **Trust-policy edges**: parse `assume_role_policy.Statement[].Principal.Service` (e.g., `"ec2.amazonaws.com"`) — this tells you *what kind of resource* is allowed to assume the role, which is a weaker/implicit signal (not a specific resource ID) but still useful for the "why does this resource exist" narrative (below): a role trusted by `ec2.amazonaws.com` with no instance actually using it is a strong "orphaned" signal for Increment 1's classifier.

### Confidence scores — a simple, defensible formula

Every edge gets a `confidence: float` (0-1), not a mystery ML score:
- `1.0` — direct ID reference (vpc_id, subnet_id, IamInstanceProfile.Arn): the two ends literally cite each other's real AWS identifier.
- `0.6` — inferred from a list membership where the target could theoretically be ambiguous (e.g., a security group appearing in `ip_permissions` as a source — matches by GroupId, so actually still `1.0`; keep this tier for cases like a trust-policy service-principal match, which names a *service*, not a specific role assumer).
- Never below `0.5` — if a rule is that uncertain, don't emit the edge at all rather than emit a low-confidence one nobody trusts. This keeps the confidence field meaningful instead of becoming visual noise.

### "Why does this resource exist" — deterministic core + one LLM narrative call

The deterministic part is just an edge traversal already available from `links`/`topological_order`: for resource X, walk `links` where X is the `target`, collect the `source` nodes and their `relation` types. That's the *evidence*. The LLM's only job is turning that evidence list into one readable sentence — same fallback-on-failure pattern as `terraform_composer.py::_compose_via_llm`:

```python
async def _explain_resource(resource: dict, incoming_edges: List[dict]) -> str:
    if not incoming_edges:
        return f"No other discovered resource references this {resource['resource_type']} - likely standalone or orphaned."
    try:
        return await ollama_client.generate(prompt=..., system="One sentence, cite only the provided edges, never invent a relationship not in the list.")
    except Exception:
        return f"Referenced by {len(incoming_edges)} resource(s): " + ", ".join(e["relation"] for e in incoming_edges)
```
Run this lazily/on-demand (a new small endpoint, not baked into the main pipeline) rather than for all N resources on every scan — an LLM call per resource would make discovery unacceptably slow given the established ~3.5 tok/s CPU-only baseline. Frontend requests an explanation only when a user clicks a specific graph node.

### Stack-boundary suggestion — deterministic first, matching the user's own stated principle

Their doc is explicit: *"Do not let the AI arbitrarily create modules. Use graph-based boundaries and deterministic heuristics first."* Concrete algorithm, buildable entirely from what `DependencyGraphBuilder` already computes:

1. **Connected components** over the *undirected* version of `links` (networkx is already a dependency — `backend/requirements.txt` — `nx.connected_components(G.to_undirected())`). Each component is a candidate stack boundary at the coarsest level — two components with zero edges between them have no reason to live in the same Terraform stack.
2. **Within a large component**, sub-partition by category using `_categorize_type()`'s existing 6 buckets, but *only* split off a category into its own file/module if doing so doesn't cut a `confidence=1.0` edge — i.e., never separate a security group from the VPC it secures into different stacks; do separate an IAM role with no `1.0`-confidence edges to anything else into its own `security/` grouping.
3. Map categories → the exact directory layout in the user's example (`foundation/` = Networking, `security/` = Security, `data/` = Database, `application/` = Compute) — this is a static category→directory-name lookup table, not a design decision the LLM makes.
4. Output shape: `{"stacks": [{"name": "foundation", "resource_ids": [...], "files": {"vpc.tf": [...], "subnets.tf": [...]}}]}` — this becomes an input to a *future* multi-file `terraform_composer` output mode (today's composer writes one flat `resources.tf`; splitting into per-stack files is itself a composer change, tracked separately from graph analysis).

**This is genuinely new scope beyond the main plan's Increment 2** (which only asked adoption planning to consume the graph, not restructure output into stacks) — recommend treating "stack-boundary suggestion + multi-file composer output" as its own increment after Increment 2 lands, not bundled into it. Flagging that explicitly rather than quietly expanding Increment 2's footprint.

---

## Part C — Migration Planning Before Generation (Adoption Planner): brainstorm

This is the piece the user's doc calls out as more valuable than showing raw HCL immediately, and it's the direct translation of "prove that importing it will not unexpectedly alter production" into something that runs *before* any Terraform even exists. Going deeper than the main plan's Increment 2 summary.

### Core insight: the Adoption Planner's job is triage, and triage needs a decision tree, not a single score

A single "risk score: 62/100" tells a user almost nothing actionable. The plan output should read like a checklist a human can act on resource-by-resource. Concrete decision tree, evaluated per resource, deterministic, no LLM:

```
Is resource.classification.category == "shared"?
  → do_not_manage (if recommended_action=="skip") or use_data_source (if recommended_action=="data_source")
Is resource.classification.category == "unsupported"?
  → unsupported
Does resource have >= 1 incoming edge with confidence 1.0 from a resource that is itself
  categorized "review_required" or "unsupported"?
  → review_required (risk propagates downstream - importing a VPC is only as safe as
    everything that depends on it also being importable cleanly)
Does resource have zero tags matching any of OWNER_TAG_KEYS/ENV_TAG_KEYS (Part A)?
  → review_required (unowned resources are exactly the ones someone should look at
    before silently adopting into IaC - this is a genuinely useful, cheap heuristic)
Is resource.resource_type in {"aws_db_instance"} (i.e., stateful, expensive-to-recreate-if-wrong)?
  → review_required, regardless of anything else - databases get a human look by default
Otherwise:
  → safe_to_import
```

This is more useful than the flat 5-bucket description in the main plan increment because it's *explainable per-resource* — the `AdoptionCategoryPlan`/`ResourceClassification` models should carry a `reason: str` populated by which branch of this tree fired, not just the resulting category. A user seeing "review_required: no owner tag" vs "review_required: depends on a resource under review" understands two very different situations from the same bucket.

### Risk score — make it mean something operationally, not just aggregate

Rather than an abstract 0-100 with an arbitrary formula, tie it to something a reviewer would actually ask: **"how many resources can I import with zero review, and how many need my attention?"**

```python
risk_score = round(100 * (review_required_count + unsupported_count) / max(total_count, 1))
```
This is a *percentage of the estate that needs a human look*, not a weighted point system pulled from nowhere — directly interpretable ("38% of your resources need review before import" is a sentence a platform engineer can act on immediately), and it composes naturally with the per-category counts already in `AdoptionPlan` (no separate weighting table to maintain or explain later, unlike `policy_agent.py`'s severity-weighted formula which the main plan suggested mirroring — on reflection, mirroring that pattern here would produce a *less* interpretable number for a fundamentally different question; recommend diverging from that pattern deliberately, and say so explicitly rather than blindly reusing it for consistency's sake).

### Import order — already computed, just needs consuming

`topological_order` already exists on `dependency_graph` (`graph_builder.py`) — the Adoption Planner doesn't need to compute import order itself, it needs to *filter* the existing topological order down to just the `safe_to_import` + `review_required` resource IDs (skip `do_not_manage`/`unsupported` entries) and expose that filtered ordering directly as `AdoptionPlan.import_order: List[str]`. This is the exact sequence `documentation_agent`'s `import_plan.md` should present the `import {}` blocks in (Increment 3) — a real, concrete connection between graph output and generated migration docs that's currently unexploited (today's `import_cmds` loop just iterates `resources` in whatever order `AWSScanner.scan_all` happened to return them, not a dependency-safe order).

### Worked example (grounded in TerraAgent's actual field names, not the strategy doc's generic example)

```
Adoption Plan — Job job-a1b2c3d4e5f6
Region: us-east-1 | Total discovered: 14 resources

safe_to_import (9)
  aws_vpc.terraagent_main_vpc_7f3a2b1c        — no dependents under review, tagged Owner=platform
  aws_subnet.public_subnet_1_9c4d5e2f         — depends on vpc above (safe)
  aws_instance.web_server_3a1b8c7d            — tagged Owner=platform, Environment=production
  ...

review_required (4)
  aws_security_group.default_a8f2c9e1         — reason: no owner tag; also flagged shared (see below)
  aws_db_instance.orders_db_1e4f6a2b          — reason: stateful resource type, always reviewed
  aws_iam_role.legacy_deploy_role_5c9d3e8f    — reason: zero incoming edges (orphaned) + no owner tag
  ...

do_not_manage (1)
  aws_security_group.default_a8f2c9e1         — reason: AWS auto-created default SG (also appears
                                                 above under review_required if it has real rules
                                                 attached beyond the AWS default - a resource can
                                                 legitimately need BOTH flags; the plan should not
                                                 force a single category when two independent
                                                 concerns both apply)

unsupported (0)

Risk score: 29% (4 of 14 resources need review before import)
Recommended import order: terraagent_main_vpc_7f3a2b1c, public_subnet_1_9c4d5e2f, ...
```
The `default_a8f2c9e1` appearing conceptually in two buckets in the worked example above surfaces a real modeling question worth deciding explicitly now rather than discovering it mid-implementation: **should `AdoptionCategoryPlan` categories be mutually exclusive per resource, or can a resource carry multiple independent flags?** Recommend: keep the five categories mutually exclusive for the *primary* recommended action (one resource → one row in the plan, one `recommended_action`), but let `ResourceClassification.reason` (Increment 1's model) be a *list* of reason strings rather than a single string, so "shared AND has real custom rules" can both surface without inventing a sixth category. This is a small but real refinement to the main plan's Increment 1 model worth making now before the Pydantic models are written.

### Where this plugs into approval / the eventual PR

The adoption plan is naturally the first thing a reviewer should see — before HCL, before validation output, before security findings. Concretely: the eventual GitHub PR body (Increment 7 in the main plan) should open with the adoption plan's category breakdown and risk score as its first section, exactly mirroring the "Summary" block in the user's own example PR structure (`148 resources discovered / 143 selected for management / 4 excluded / 1 requires manual review`) — that example maps directly onto `AdoptionPlan.categories` counts with zero translation needed, confirming the model shape proposed in Increment 0 is already PR-body-ready.

---

## Summary of what's genuinely new here vs. the main Phase 1 plan

- **Part A** adds: normalized `inventory` sub-object (owner/environment/age, tag-based only), the exact per-API-call reality check on what creation-time data is actually available, and a concrete cross-region scanning mechanism (`multi_region_scanner.py`) — none of this was in the main plan's Increment 0-8 list; recommend it as a new Increment 1.5, buildable independently after classification lands.
- **Part B** adds: IAM instance-profile edges (needs one new field read in `aws_scanner.py`, zero new API calls), a defensible confidence-score tiering, the deterministic-first stack-boundary algorithm — and explicitly flags that ALB/ECS/KMS-style "data-plane" edges are blocked on discovery breadth that doesn't exist yet, so don't promise them as part of "improve the graph."
- **Part C** replaces the main plan's flat category-count risk score with an operationally-interpretable one (`% needing review`, not a weighted point total), turns `topological_order` (already computed, currently unused by the composer) into the actual import ordering, and resolves a real modeling ambiguity (can a resource carry multiple classification reasons) before it becomes a Pydantic schema that's awkward to change later.
