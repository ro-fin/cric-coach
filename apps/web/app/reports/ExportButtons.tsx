"use client";

/**
 * US-K5 export controls: server-side A4 PDF/PNG via the /reports/{id}/export
 * endpoint, plus the browser-print path styled by the print stylesheet.
 *
 * The export endpoint is bearer-gated (HTTPBearer, header-only), so a bare
 * <a href> could never authenticate — every click would 401. Instead the
 * export is fetched WITH the Authorization header and delivered through a
 * blob object URL download. Hidden on paper via `.no-print`.
 */

import { useState } from "react";
import { authHeaders } from "@/lib/api";
import { exportUrl } from "./api";

export type ExportFormat = "pdf" | "png";

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
    <p className="no-print" data-testid="export-buttons">
      <button type="button" onClick={() => void handleExport("pdf")}>
        Export PDF
      </button>{" "}
      <button type="button" onClick={() => void handleExport("png")}>
        Export PNG
      </button>{" "}
      <button type="button" onClick={() => window.print()}>
        Print
      </button>
      {error !== null && <span role="alert"> {error}</span>}
    </p>
  );
}
