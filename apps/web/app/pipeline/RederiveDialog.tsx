"use client";

/**
 * Re-derive confirmation (Phase-3 debt, parent only). Re-deriving deletes the
 * session's machine-made rows and runs the pipeline again. If manual rows
 * (tags, corrections) would be lost, the server refuses and lists them; the
 * parent must then tick an explicit "delete these too" box before a second,
 * confirmed request. Every server answer is shown verbatim.
 */

import { useState } from "react";
import { Button, Dialog, useToast } from "@/components/ui";
import type { PipelineApi, RederiveOut } from "./api";

export interface RederiveDialogProps {
  api: PipelineApi;
  sessionId: string;
  sessionLabel: string;
  onClose: () => void;
  /** Called after a successful cascade with the server's answer. */
  onDone: (out: RederiveOut) => void;
}

/** "findings_deleted 3, clips_deleted 12": non-zero counts, served keys verbatim. */
export function countsText(counts: Record<string, unknown>): string {
  const parts = Object.entries(counts)
    .filter(([, value]) => value !== 0)
    .map(([key, value]) => `${key} ${typeof value === "number" ? value : JSON.stringify(value)}`);
  return parts.length === 0 ? "nothing needed deleting" : parts.join(", ");
}

export function RederiveDialog({ api, sessionId, sessionLabel, onClose, onDone }: RederiveDialogProps) {
  const { toast } = useToast();
  const [blocked, setBlocked] = useState<{ message: string; blockers: Record<string, number> } | null>(
    null,
  );
  const [confirmManual, setConfirmManual] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = () => {
    setBusy(true);
    setProblem(null);
    api
      .rederive(sessionId, { confirm_manual_invalidation: confirmManual, start_run: true })
      .then((result) => {
        if (result.kind === "blocked") {
          setBlocked({ message: result.message, blockers: result.blockers });
          return;
        }
        const out = result.out;
        toast({
          title: out.manual_invalidation ? "Re-derived, manual rows deleted" : "Re-derived",
          description: countsText(out.counts),
          tone: "success",
        });
        if (out.run_locked) {
          toast({
            title: "Fresh run skipped",
            description: "Another worker holds this session; use Resume when it finishes.",
            tone: "warning",
          });
        }
        onDone(out);
      })
      .catch((error: unknown) => {
        setProblem(error instanceof Error ? error.message : String(error));
      })
      .finally(() => setBusy(false));
  };

  const needsConfirmation = blocked !== null && !confirmManual;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) {
          onClose();
        }
      }}
      title="Re-derive this session?"
    >
      <div className="flex flex-col gap-4 text-ink">
        <p>
          {`${sessionLabel}: the machine-made results are deleted and the pipeline runs again from the recorded video.`}
        </p>
        <p className="text-ink-muted">Manual tags and corrections are kept unless you confirm below.</p>
        {problem !== null && (
          <p role="alert" className="font-semibold text-danger">
            {problem}
          </p>
        )}
        {blocked !== null && (
          <div role="alert" data-testid="rederive-blockers" className="rounded-xl border-2 border-warning px-4 py-3">
            <p className="font-semibold text-warning">Manual work would be lost</p>
            <ul className="mt-1 list-disc pl-6">
              {Object.entries(blocked.blockers).map(([label, count]) => (
                <li key={label}>{`${label}: ${count}`}</li>
              ))}
            </ul>
            <p className="mt-2 text-sm text-ink-muted">{blocked.message}</p>
            <label className="mt-3 flex min-h-11 cursor-pointer items-center gap-3 font-semibold">
              <input
                type="checkbox"
                checked={confirmManual}
                onChange={(event) => setConfirmManual(event.target.checked)}
                className="size-5 accent-danger"
              />
              Delete these manual rows too. This cannot be undone.
            </label>
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="danger" loading={busy} disabled={needsConfirmation} onClick={run}>
            Re-derive
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
