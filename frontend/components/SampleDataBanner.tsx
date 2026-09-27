import { FlaskConical } from "lucide-react";

/**
 * Marks a restored preview screen whose content is illustrative, not read from
 * the backend. Keeps a demo audience from mistaking sample numbers for real
 * results from their AWS account.
 */
export default function SampleDataBanner({ feature }: { feature: string }) {
  return (
    <div className="flex items-start gap-2.5 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
      <FlaskConical className="w-4 h-4 shrink-0 mt-0.5 text-amber-600" />
      <p>
        <span className="font-bold">Sample data.</span> {feature} is a preview of a planned feature. The content below
        is illustrative and is not read from your AWS account or GitHub.
      </p>
    </div>
  );
}
