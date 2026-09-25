"use client";

import { useEffect, useRef, useState } from "react";
import * as d3 from "d3";
import { Download } from "lucide-react";

interface GraphNode extends d3.SimulationNodeDatum {
  id: string;
  name: string;
  type: string;
  degree?: number;
  tags?: Array<{ Key: string; Value: string }>;
}

interface GraphLink extends d3.SimulationLinkDatum<GraphNode> {
  source: string | GraphNode;
  target: string | GraphNode;
  relation?: string;
}

interface DependencyGraphProps {
  data: {
    nodes: GraphNode[];
    links: GraphLink[];
  };
  // Full-screen page: fill most of the viewport instead of a fixed 480px.
  tall?: boolean;
}

const TYPE_COLORS: Record<string, string> = {
  aws_vpc: "#8b5cf6",
  aws_subnet: "#3b82f6",
  aws_instance: "#60a5fa",
  aws_security_group: "#ef4444",
  aws_s3_bucket: "#10b981",
  aws_db_instance: "#f59e0b",
  aws_iam_role: "#ec4899",
  default: "#94a3b8",
};

const CANVAS_BACKGROUND = "#090d16";

interface TooltipState {
  x: number;
  y: number;
  node: GraphNode;
}

export default function DependencyGraph({ data, tall = false }: DependencyGraphProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);

  useEffect(() => {
    if (!svgRef.current || !data?.nodes?.length) return;

    const width = 800;
    const height = 480;

    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove(); // Clear previous renders

    // Background rect so exported PNGs match the app's dark theme instead of
    // rendering transparent.
    svg.append("rect").attr("width", width).attr("height", height).attr("fill", CANVAS_BACKGROUND);

    const g = svg.append("g");

    // Zoom behavior
    svg.call(
      d3.zoom<SVGSVGElement, unknown>()
        .scaleExtent([0.3, 3])
        .on("zoom", (event) => {
          g.attr("transform", event.transform);
        })
    );

    // Deep clone data for simulation
    const nodes: GraphNode[] = data.nodes.map((d) => ({ ...d }));
    const links: GraphLink[] = data.links.map((d) => ({ ...d }));

    const simulation = d3.forceSimulation<GraphNode>(nodes)
      .force("link", d3.forceLink<GraphNode, GraphLink>(links).id((d) => d.id).distance(100))
      .force("charge", d3.forceManyBody().strength(-300))
      .force("center", d3.forceCenter(width / 2, height / 2));

    // Links
    const link = g.append("g")
      .attr("stroke", "#334155")
      .attr("stroke-opacity", 0.6)
      .attr("stroke-width", 2)
      .selectAll("line")
      .data(links)
      .join("line");

    // Nodes
    const node = g.append("g")
      .selectAll<SVGGElement, GraphNode>("g")
      .data(nodes)
      .join("g")
      .call(
        d3.drag<SVGGElement, GraphNode>()
          .on("start", (event, d) => {
            if (!event.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
          })
          .on("drag", (event, d) => {
            d.fx = event.x;
            d.fy = event.y;
          })
          .on("end", (event, d) => {
            if (!event.active) simulation.alphaTarget(0);
            d.fx = null;
            d.fy = null;
          })
      )
      .on("mouseenter", (event, d) => {
        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) return;
        setTooltip({ x: event.clientX - rect.left, y: event.clientY - rect.top, node: d });
      })
      .on("mousemove", (event) => {
        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) return;
        setTooltip((prev) => (prev ? { ...prev, x: event.clientX - rect.left, y: event.clientY - rect.top } : prev));
      })
      .on("mouseleave", () => setTooltip(null));

    node.append("circle")
      .attr("r", 14)
      .attr("fill", (d) => TYPE_COLORS[d.type] || TYPE_COLORS.default)
      .attr("stroke", "#ffffff")
      .attr("stroke-width", 1.5)
      .attr("class", "cursor-pointer transition-transform hover:scale-125");

    node.append("text")
      .text((d) => d.name || d.id)
      .attr("x", 18)
      .attr("y", 4)
      .attr("fill", "#cbd5e1")
      .attr("font-size", "11px")
      .attr("font-family", "monospace");

    simulation.on("tick", () => {
      // d3.forceLink() resolves source/target from string ids to the actual
      // GraphNode objects (with .x/.y set) in place once the simulation runs,
      // so by the time tick fires these are safe to treat as GraphNode.
      link
        .attr("x1", (d) => (d.source as GraphNode).x ?? 0)
        .attr("y1", (d) => (d.source as GraphNode).y ?? 0)
        .attr("x2", (d) => (d.target as GraphNode).x ?? 0)
        .attr("y2", (d) => (d.target as GraphNode).y ?? 0);

      node.attr("transform", (d) => `translate(${d.x},${d.y})`);
    });

    return () => {
      simulation.stop();
    };
  }, [data]);

  const exportPng = () => {
    const svgEl = svgRef.current;
    if (!svgEl) return;

    // The live SVG has no width/height attributes (only viewBox + CSS w-full/h-full),
    // so serialized standalone - with no parent element to size against - it has no
    // intrinsic dimensions. Loading that via Image() falls back to the browser's
    // default (often 300x150), producing a tiny/broken export. Set explicit
    // width/height on a clone before serializing; CSS still wins for the on-screen
    // element, so this doesn't affect the live, responsive graph.
    const exportWidth = 800;
    const exportHeight = 480;
    const clone = svgEl.cloneNode(true) as SVGSVGElement;
    clone.setAttribute("width", String(exportWidth));
    clone.setAttribute("height", String(exportHeight));

    const serializer = new XMLSerializer();
    const svgString = serializer.serializeToString(clone);
    const svgBlob = new Blob([svgString], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(svgBlob);

    const img = new Image();
    img.onload = () => {
      const scale = 2; // export at 2x for a crisper image
      const canvas = document.createElement("canvas");
      canvas.width = exportWidth * scale;
      canvas.height = exportHeight * scale;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        URL.revokeObjectURL(url);
        return;
      }
      ctx.fillStyle = CANVAS_BACKGROUND;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0);
      URL.revokeObjectURL(url);

      canvas.toBlob((blob) => {
        if (!blob) return;
        const pngUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = pngUrl;
        a.download = "terraagent-dependency-graph.png";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(pngUrl);
      }, "image/png");
    };
    img.src = url;
  };

  const presentTypes = Array.from(new Set((data?.nodes ?? []).map((n) => n.type)));
  const legend = presentTypes
    .filter((t) => t in TYPE_COLORS)
    .sort()
    .map((t) => ({ label: t.replace("aws_", "").replace(/_/g, " "), color: TYPE_COLORS[t] }));
  if (presentTypes.some((t) => !(t in TYPE_COLORS))) {
    legend.push({ label: "other", color: TYPE_COLORS.default });
  }

  return (
    <div className="card overflow-hidden relative">
      <div className="px-4 py-3 border-b border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-x-3 gap-y-1.5 flex-wrap text-2xs">
          {legend.length === 0 ? (
            <span className="text-slate-400">No resources to plot</span>
          ) : (
            legend.map((l) => (
              <span key={l.label} className="flex items-center gap-1.5 text-slate-600 capitalize">
                <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: l.color }} />
                {l.label}
              </span>
            ))
          )}
        </div>
        <button onClick={exportPng} className="btn-secondary py-1.5 px-3 text-2xs self-start sm:self-auto shrink-0">
          <Download className="w-3.5 h-3.5" />
          Export PNG
        </button>
      </div>

      <div
        ref={containerRef}
        className={`w-full bg-[#090d16] flex items-center justify-center relative ${
          tall ? "h-[calc(100vh-15rem)] min-h-[480px]" : "h-[480px]"
        }`}
      >
        {!data?.nodes?.length && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-slate-500">
            No dependency data for this job.
          </div>
        )}
        <svg ref={svgRef} viewBox="0 0 800 480" className="w-full h-full" />

        {tooltip && (
          <div
            className="absolute z-10 pointer-events-none px-3 py-2 rounded-lg bg-[#0d121c] border border-slate-700 shadow-xl text-2xs max-w-xs"
            style={{ left: tooltip.x + 16, top: tooltip.y + 16 }}
          >
            <div className="font-semibold text-slate-100">{tooltip.node.name || tooltip.node.id}</div>
            <div className="font-mono text-brand-400">{tooltip.node.id}</div>
            <div className="text-slate-400">{tooltip.node.type}</div>
            {tooltip.node.tags && tooltip.node.tags.length > 0 && (
              <div className="mt-1 pt-1 border-t border-slate-700/60 space-y-0.5">
                {tooltip.node.tags.map((tag) => (
                  <div key={tag.Key} className="text-slate-300">
                    <span className="text-slate-500">{tag.Key}:</span> {tag.Value}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
