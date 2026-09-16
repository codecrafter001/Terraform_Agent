import ScanProgress from "@/components/ScanProgress";

interface ScanProgressPageProps {
  params: {
    id: string;
  };
}

export default function ScanProgressPage({ params }: ScanProgressPageProps) {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-ink">
          Active Scan Job: <span className="font-mono text-brand-600">{params.id}</span>
        </h1>
        <p className="text-gray-500 text-sm">
          Real-time multi-agent execution pipeline streaming from LangGraph orchestrator.
        </p>
      </div>

      <ScanProgress jobId={params.id} />
    </div>
  );
}
