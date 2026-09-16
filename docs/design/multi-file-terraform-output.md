# Implementation Plan: Multi-File Terraform Output (Stack-Based Splitting)

## Context

`tools/graph_builder.py` already computes `dependency_graph["stacks"]` — groupings like
`{"name": "foundation", "resource_ids": [...]}` (connected components, sub-partitioned by
category: Networking→foundation, Security→security, Database/Storage→data, Compute/General→application).
This was built and verified as part of the "Dependency Graph — deeper relationships" work, but
nothing consumes it yet: `terraform_composer_node` still writes every resource into one flat
`resources.tf`, ignoring the stack data entirely.

This plan makes the composer actually split its output into `foundation.tf`, `security.tf`,
`data.tf`, `application.tf` (only the ones that actually get resources), while keeping
`providers.tf` / `variables.tf` / `outputs.tf` as shared, single files. This is safe by
construction: Terraform merges every `.tf` file in a directory into one root-module scope, so
splitting by file is purely organizational, never a functional risk — no reference can break
just because two resources live in different files in the same directory.

## Step 1 — `backend/agents/terraform_composer.py`: route each block by stack

Currently `resource_blocks = []` and `output_blocks = []` are flat lists; every
`resource_blocks.append(...)` throughout the big `if/elif` chain (VPC, subnet, S3, security
group, instance, route table, LLM fallback) appends to that one list.

**Change:** build a reverse lookup once, before the resource loop:
```python
stack_by_resource_id: Dict[str, str] = {
    rid: stack["name"]
    for stack in (state.get("dependency_graph") or {}).get("stacks", [])
    for rid in stack["resource_ids"]
}
```
Replace the two flat lists with per-stack dicts:
```python
resource_blocks_by_stack: Dict[str, List[str]] = {}
output_blocks_by_stack: Dict[str, List[str]] = {}
```
Inside the loop, right after `clean_name = unique_clean_name(...)`, resolve the target file:
```python
stack_name = stack_by_resource_id.get(r_id, "application")  # same fallback graph_builder.py uses
```
Then replace every `resource_blocks.append(X)` in the whole `if/elif` chain with
`resource_blocks_by_stack.setdefault(stack_name, []).append(X)`, and the one
`output_blocks.append(...)` (VPC ID output) with
`output_blocks_by_stack.setdefault(stack_name, []).append(X)`. This is a mechanical
find-and-replace across the existing chain — no HCL-generation logic changes, just which list
it lands in. The `skip` / `data_source` branches at the top of the loop route through the same
`resource_blocks_by_stack` dict, so classification-driven skipping/data-sourcing keeps working
exactly as it does today, just now stack-aware too.

**Assemble the files** (replacing the current single `resources_tf` / `outputs_tf` join):
```python
terraform_files = {
    "providers.tf": providers_tf,
    "variables.tf": variables_tf,
}
for stack_name, blocks in resource_blocks_by_stack.items():
    terraform_files[f"{stack_name}.tf"] = "\n\n".join(blocks)
if any(output_blocks_by_stack.values()):
    all_outputs = [b for blocks in output_blocks_by_stack.values() for b in blocks]
    terraform_files["outputs.tf"] = "\n\n".join(all_outputs)
```
Outputs stay in one combined `outputs.tf` rather than being split too — outputs are a
cross-cutting concern, not something that benefits from being scattered across stack files, and
keeping them combined avoids an empty-outputs-file edge case per stack.

## Step 2 — `backend/agents/repair_agent.py`: the real ripple effect

This is the part that actually breaks if left alone. Today it hardcodes `tf_files["resources.tf"]`
in three places: the S3-encryption patch reads/appends to it, `_extract_block` searches within
it, and the fixed content gets written back to that one key. Once Step 1 lands, that key won't
exist.

**Fix:** operate across every generated resource-bearing file, not one fixed key.
```python
STACK_FILE_NAMES = {"foundation.tf", "security.tf", "data.tf", "application.tf"}
resource_file_names = [f for f in tf_files if f in STACK_FILE_NAMES]
```
- **S3-encryption patch** (currently one `resources_content` string): loop over
  `resource_file_names`, run the existing bucket-name regex + append-if-missing logic against
  *each* file's content independently, writing each back to `tf_files[filename]` — a bucket
  only ever lives in one stack file, so this doesn't need cross-file awareness, just repeating
  the same per-file logic instead of assuming a single file.
- **LLM-driven patch** (`_extract_block` + replace): for each actionable finding, the target
  resource could be in *any* stack file — try `_extract_block` against each
  `resource_file_names` entry in turn, stop at the first match, apply the fix to *that* file's
  content, write it back. A resource address is only ever found in one file, so this is a
  simple linear search, not a combinatorial problem.

## Step 3 — `backend/agents/documentation_agent.py`: stop hardcoding the file list

The README-generation system prompt hardcodes the literal string
`"providers.tf, variables.tf, resources.tf, outputs.tf"` as the exact whitelist the LLM must
describe. Once the file set is dynamic (some scans might produce only `foundation.tf` +
`security.tf`, others all four stack files), this static string goes stale and the README will
describe files that don't exist.

**Fix:** build that fragment of the prompt from the actual `state["terraform_files"].keys()` at
call time instead of a fixed string:
```python
tf_file_list = ", ".join(sorted(state.get("terraform_files", {}).keys()))
...
f"these real files - never invent others such as terraform.tfstate or "
f"terraform.tfvars, which this tool never creates: {tf_file_list} (all in terraform/), "
"inventory.json, ..."
```

## Step 4 — Confirmed to need *no* change, and why

- `backend/tools/terraform_runner.py` (`format_hcl`, `validate_hcl`) — both already iterate
  `hcl_files.items()` generically and run `terraform fmt` / `init` / `validate` against the
  whole sandbox directory. More files, same behavior.
- `backend/tools/zip_builder.py` — `build_zip` already writes every `tf_files` entry under
  `terraform/<filename>` in a loop. No count assumption anywhere.
- `backend/agents/validation_agent.py` — operates on whatever `TerraformRunner.validate_hcl`
  returns; doesn't inspect filenames itself.
- Native `import {}` block generation (a separate, earlier-proposed increment) — never actually
  got built, so there's no `import_blocks.tf` dependency to worry about here.

## Verification plan

1. Unit test on the new `resource_blocks_by_stack` routing: synthetic resources spanning 2+
   categories, assert `terraform_files` contains `foundation.tf` and `application.tf` as
   separate keys with the right resources in each, and that a `skip`-classified resource still
   produces zero output anywhere.
2. Unit test on `repair_agent.py`'s updated multi-file search: a finding whose address lives in
   `security.tf` (not `foundation.tf`), assert the fix lands in the correct file and the others
   are untouched.
3. Real `terraform validate` smoke test (no AWS creds needed, same pattern as existing
   validation tests): generate a multi-file output with resources split across 2 stacks,
   confirm `terraform fmt` / `init` / `validate` all still pass against the directory as a
   whole — proving the "multiple files, one root module" assumption holds in practice, not just
   in theory.
4. Live full-pipeline run (same pattern used for every other increment this session): confirm
   `final_state["terraform_files"]` keys match the stacks actually produced by that run's
   `dependency_graph["stacks"]`, and that the README's file-structure section lists exactly
   those files.
