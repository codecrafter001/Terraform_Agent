import ScanProgress from "@/components/ScanProgress";
import { PageHeader } from "@/components/ui";

interface ScanProgressPageProps {
  params: Promise<{
    id: string;
  }>;
}

export default async function ScanProgressPage({ params }: ScanProgressPageProps) {
  const { id } = await params;
  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Scan progress" }]}
        title={
          <>
            <span>Scan in progress</span>
            <span className="font-mono text-xs font-semibold text-brand-700 bg-brand-50 px-2.5 py-1 rounded-lg border border-brand-200">
              {id}
            </span>
          </>
        }
        description="Four agents, with a verify ⇄ repair loop that re-checks every fix."
      />

      <ScanProgress jobId={id} />
    </div>
  );
}
