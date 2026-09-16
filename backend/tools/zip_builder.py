"""ZIP packaging tool for exporting generated Terraform codebase and documentation."""

import csv
import hashlib
import io
import json
import logging
import os
import zipfile
from typing import Any, Dict, List, Optional

import pyzipper

logger = logging.getLogger("terraagent.zip_builder")

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/tmp/terraagent")

TYPE_COLORS = {
    "aws_vpc": "#8b5cf6",
    "aws_subnet": "#3b82f6",
    "aws_instance": "#60a5fa",
    "aws_security_group": "#ef4444",
    "aws_s3_bucket": "#10b981",
    "aws_db_instance": "#f59e0b",
    "aws_iam_role": "#ec4899",
}
DEFAULT_COLOR = "#94a3b8"


def _build_inventory_csv(inventory: Dict[str, Any]) -> str:
    """Flatten the discovered-resource inventory into a CSV for spreadsheet review."""
    resources = inventory.get("resources", []) or []
    if not resources:
        return "resource_type,id,name\n"

    priority_cols = ["resource_type", "id", "name"]
    all_cols: List[str] = list(priority_cols)
    for res in resources:
        for key in res.keys():
            if key not in all_cols:
                all_cols.append(key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_cols, extrasaction="ignore")
    writer.writeheader()
    for res in resources:
        row = {}
        for col in all_cols:
            val = res.get(col)
            row[col] = json.dumps(val, default=str) if isinstance(val, (list, dict)) else val
        writer.writerow(row)
    return buf.getvalue()


def _build_dependency_graph_html(graph_data: Dict[str, Any]) -> str:
    """A standalone, single-file D3 force-directed graph - opens directly in a
    browser, no server or web app required. Uses the same visual language
    (colors, layout) as the live frontend's DependencyGraph component."""
    # Resource names/tags come straight from the scanned AWS account - anyone
    # with tag-write permission there controls this string. Escaping "<" stops
    # a value like "</script><script>..." from closing this block early and
    # injecting a live script tag (verified: json.dumps alone does not escape
    # this). This file is designed to be opened directly in a browser with no
    # server, so there's no other layer that would catch this.
    graph_json = json.dumps(graph_data, default=str).replace("<", "\\u003c")
    colors_json = json.dumps({**TYPE_COLORS, "default": DEFAULT_COLOR})
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TerraAgent Dependency Graph</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<style>
  body {{ margin: 0; background: #090d16; font-family: -apple-system, sans-serif; }}
  #legend {{ position: fixed; top: 12px; left: 12px; display: flex; gap: 14px; flex-wrap: wrap; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; color: #cbd5e1; font-size: 11px; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
  text {{ fill: #cbd5e1; font-size: 11px; font-family: monospace; }}
  line {{ stroke: #334155; stroke-opacity: 0.6; stroke-width: 2; }}
  circle {{ stroke: #ffffff; stroke-width: 1.5; cursor: pointer; }}
</style>
</head>
<body>
<div id="legend"></div>
<svg id="graph" width="100%" height="100vh"></svg>
<script>
  const data = {graph_json};
  const colors = {colors_json};
  const legend = document.getElementById("legend");
  Object.entries(colors).filter(([k]) => k !== "default").forEach(([type, color]) => {{
    const item = document.createElement("div");
    item.className = "legend-item";
    item.innerHTML = `<span class="legend-dot" style="background:${{color}}"></span>${{type.replace("aws_", "").toUpperCase()}}`;
    legend.appendChild(item);
  }});

  const svg = d3.select("#graph");
  const width = window.innerWidth, height = window.innerHeight;
  const g = svg.append("g");
  svg.call(d3.zoom().scaleExtent([0.3, 3]).on("zoom", (e) => g.attr("transform", e.transform)));

  const nodes = (data.nodes || []).map(d => ({{...d}}));
  const links = (data.links || []).map(d => ({{...d}}));

  const simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(100))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(width / 2, height / 2));

  const link = g.append("g").selectAll("line").data(links).join("line");

  const node = g.append("g").selectAll("g").data(nodes).join("g")
    .call(d3.drag()
      .on("start", (event, d) => {{ if (!event.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
      .on("drag", (event, d) => {{ d.fx = event.x; d.fy = event.y; }})
      .on("end", (event, d) => {{ if (!event.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }}));

  node.append("circle").attr("r", 14).attr("fill", d => colors[d.type] || colors.default);
  node.append("text").text(d => d.name || d.id).attr("x", 18).attr("y", 4);
  node.append("title").text(d => {{
    let t = d.id + " (" + d.type + ")";
    if (d.tags && d.tags.length) t += "\\n" + d.tags.map(tag => tag.Key + "=" + tag.Value).join(", ");
    return t;
  }});

  simulation.on("tick", () => {{
    link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
  }});
</script>
</body>
</html>
"""


class ZipBuilder:
    @staticmethod
    def build_zip(
        job_id: str,
        tf_files: Dict[str, str],
        inventory: Dict[str, Any],
        graph_data: Dict[str, Any],
        validation_results: Dict[str, Any],
        security_results: Dict[str, Any],
        cost_results: Optional[Dict[str, Any]] = None,
        pending_approval: Optional[Dict[str, Any]] = None,
        drift_results: Optional[Dict[str, Any]] = None,
        generation_manifest: Optional[Dict[str, Any]] = None,
        docs: Optional[Dict[str, str]] = None,
        output_dir: Optional[str] = None,
        password: Optional[str] = None
    ) -> Dict[str, Any]:
        """Package Terraform HCL, discovery inventory, dependency graph, security/validation
        reports, generation manifest, and documentation into a single downloadable ZIP bundle.

        If `password` is given, the bundle is AES-256 encrypted (via pyzipper -
        the stdlib zipfile module can only produce the legacy, trivially-crackable
        ZipCrypto encryption, never real AES) and every entry, including
        directory/file names, requires that password to extract.

        Returns {zip_path, sha256, manifest} - the manifest and checksum let the
        frontend show exactly what's in the bundle and let a user verify the
        download wasn't corrupted/tampered with in transit."""
        target_dir = output_dir or OUTPUT_DIR
        os.makedirs(target_dir, exist_ok=True)
        zip_path = os.path.join(target_dir, f"{job_id}.zip")

        zip_cls = pyzipper.AESZipFile if password else zipfile.ZipFile
        open_kwargs = {"compression": zipfile.ZIP_DEFLATED}
        if password:
            open_kwargs["encryption"] = pyzipper.WZ_AES

        with zip_cls(zip_path, "w", **open_kwargs) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))

            for filename, content in tf_files.items():
                zf.writestr(os.path.join("terraform", filename), content)

            if docs:
                for filename, content in docs.items():
                    zf.writestr(filename, content)

            zf.writestr("inventory.json", json.dumps(inventory, indent=2, default=str))
            zf.writestr("inventory.csv", _build_inventory_csv(inventory))
            zf.writestr("dependency_graph.json", json.dumps(graph_data, indent=2, default=str))
            zf.writestr("dependency_graph.html", _build_dependency_graph_html(graph_data))
            zf.writestr("reports/validation_report.json", json.dumps(validation_results, indent=2, default=str))
            zf.writestr("reports/security_report.json", json.dumps(security_results, indent=2, default=str))
            zf.writestr("reports/cost_report.json", json.dumps(cost_results or {}, indent=2, default=str))
            zf.writestr("reports/pending_approval.json", json.dumps(pending_approval or {}, indent=2, default=str))
            zf.writestr("reports/drift_results.json", json.dumps(drift_results or {}, indent=2, default=str))
            if generation_manifest:
                zf.writestr("reports/generation_manifest.json", json.dumps(generation_manifest, indent=2, default=str))

        manifest: List[Dict[str, Any]] = []
        with zip_cls(zip_path, "r") as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
            for info in zf.infolist():
                manifest.append({"name": info.filename, "size": info.file_size})

        sha256 = hashlib.sha256()
        with open(zip_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)

        logger.info(f"Created ZIP bundle at {zip_path} for Job {job_id}")
        return {
            "zip_path": zip_path,
            "sha256": sha256.hexdigest(),
            "manifest": manifest
        }
