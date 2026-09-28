"use client";

/**
 * US-K5 export controls: server-side A4 PDF/PNG via the /reports/{id}/export
 * endpoint, plus two browser-print paths styled by the report stylesheet:
 * the one-page net-wall sheet and the full report.
 *
 * The export endpoint is auth-gated, so a bare <a href> is not used: the
 * export is fetched with the shared auth headers (the same-origin proxy adds
 * the bearer server-side) and delivered through a blob object URL download.
 * Hidden on paper via `.no-print`.
 */

import { Download, Printer } from "lucide-react";
import { useState } from "react";
import { Button, ErrorState } from "@/components/ui";
import { authHeaders } from "@/lib/api";
import { exportUrl } from "./api";

export type ExportFormat = "pdf" | "png";
export type PrintMode = "wall" | "full";

async function downloadExport(reportId: string, format: ExportFormat): Promise<void> {
  const response = await fetch(exportUrl(reportId, format), { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`export failed: ${response.status}`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `report-${reportId}.${format}`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** Print in one layout: the mode lives on <html> only for the print job. */
export function printAs(mode: PrintMode): void {
  const root = document.documentElement;
  root.dataset.printMode = mode;
  try {
    window.print();
  } finally {
    delete root.dataset.printMode;
  }
}

export default function ExportButtons({ reportId }: { reportId: string }) {
  const [error, setError] = useState<string | null>(null);

  async function handleExport(format: ExportFormat): Promise<void> {
    try {
      await downloadExport(reportId, format);
      setError(null);
    } catch {
      setError("Export failed — check the lab server connection and your access.");
    }
  }

  return (
    <div className="no-print flex flex-col gap-3" data-print="hide" data-testid="export-buttons">
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => printAs("wall")}>
          <Printer aria-hidden="true" className="size-5" />
          Print net-wall sheet
        </Button>
        <Button variant="secondary" onClick={() => printAs("full")}>
          <Printer aria-hidden="true" className="size-5" />
          Print full report
        </Button>
        <Button variant="secondary" onClick={() => void handleExport("pdf")}>
          <Download aria-hidden="true" className="size-5" />
          Export PDF
        </Button>
        <Button variant="secondary" onClick={() => void handleExport("png")}>
          <Download aria-hidden="true" className="size-5" />
          Export PNG
        </Button>
      </div>
      {error !== null && <ErrorState message={error} />}
    </div>
  );
}
