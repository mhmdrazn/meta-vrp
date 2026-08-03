// src/components/DemoDisclaimer.tsx
// Required banner text (paper-revision spec §9) — must appear verbatim.
import { Info } from 'lucide-react'

export function DemoDisclaimer() {
  return (
    <div
      role="note"
      className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-950 dark:text-amber-100 flex items-start gap-3"
    >
      <Info className="h-4 w-4 mt-0.5 shrink-0 text-amber-600 dark:text-amber-400" />
      <p>
        The public application uses a shortened demonstration configuration. Results
        reported in the study were generated through controlled offline experiments.
      </p>
    </div>
  )
}
