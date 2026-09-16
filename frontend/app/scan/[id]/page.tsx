import ScanProgress from "@/components/ScanProgress";

interface ScanProgressPageProps {
  params: Promise<{
    id: string;
  }>;
}

export default async function ScanProgressPage({ params }: ScanProgressPageProps) {
  const { id } = await params;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-ink">
          Active Scan Job: <span className="font-mono text-brand-600">{id}</span>
        </h1>
        <p className="text-gray-500 text-sm">
          Real-time multi-agent execution pipeline streaming from LangGraph orchestrator.
        </p>
      </div>

      <ScanProgress jobId={id} />
    </div>
  );
}
