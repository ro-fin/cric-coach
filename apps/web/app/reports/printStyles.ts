/**
 * US-K5 report stylesheet, injected as a <style> element by the reports page
 * (global CSS belongs to the root layout). All colours are the design tokens'
 * CSS variables, so light, dark and paper stay in step with globals.css.
 *
 * Screen: tables and the report's sections read as the rest of the app.
 *
 * Paper has two layouts, chosen by the print buttons through
 * `html[data-print-mode]`:
 * - "wall" — the net-wall sheet: one A4 page in large type with only the
 *   `data-wall="core"` parts (safety, honesty and coverage notes, the one
 *   correction, the drill, the goal, what went well). Evidence links and every
 *   `data-wall="detail"` section are left off.
 * - "full" (or plain browser print) — the whole report, sections never split
 *   across sheets.
 * `.no-print` controls (export buttons, forms) never reach paper.
 */
export const PRINT_STYLES = `
.report-view table { width: 100%; border-collapse: collapse; margin: 0.5rem 0; }
.report-view th, .report-view td {
  border-bottom: 1px solid var(--border);
  padding: 0.4rem 0.5rem;
  text-align: left;
}
.report-view th { color: var(--ink-muted); font-size: 0.875rem; }
@page { size: A4; margin: 15mm; }
@media print {
  nav, .no-print { display: none !important; }
  main { margin: 0; padding: 0; max-width: none; }
  .report-view { font-size: 12pt; line-height: 1.45; }
  .report-view section, .report-view [data-wall] { break-inside: avoid; }
  .report-view a { color: #000; text-decoration: none; }
  html[data-print-mode="wall"] .report-view [data-wall="detail"],
  html[data-print-mode="wall"] .report-view .evidence,
  html[data-print-mode="wall"] [data-print-wall="hide"] { display: none !important; }
  html[data-print-mode="wall"] .report-view { font-size: 20pt; line-height: 1.3; }
  html[data-print-mode="wall"] .report-view h1 { font-size: 30pt; }
  html[data-print-mode="wall"] .report-view h2 { font-size: 24pt; margin-top: 10mm; }
}
`;
