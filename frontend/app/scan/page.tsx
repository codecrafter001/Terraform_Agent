import CredentialForm from "@/components/CredentialForm";
import { PageHeader } from "@/components/ui";

export default function ScanLauncherPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "New scan" }]}
        title="New Scan & Discovery"
        description="Discover AWS resources read-only and generate validated Terraform or OpenTofu code."
      />

      <CredentialForm />
    </div>
  );
}
