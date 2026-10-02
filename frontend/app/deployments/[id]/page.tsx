import DeploymentDetailView from "@/components/DeploymentDetailView";
import { PageHeader } from "@/components/ui";

interface DeploymentPageProps {
  params: Promise<{ id: string }>;
}

export default async function DeploymentPage({ params }: DeploymentPageProps) {
  const { id } = await params;
  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Deployments", href: "/deployments" }, { label: id }]}
        title={
          <>
            <span>Deployment</span>
            <span className="font-mono text-xs font-semibold text-brand-700 bg-brand-50 px-2.5 py-1 rounded-lg border border-brand-200">
              {id}
            </span>
          </>
        }
        description="Analyze → choose a target → build, render and verify. Nothing here changes your AWS account."
      />
      <DeploymentDetailView id={id} />
    </div>
  );
}
