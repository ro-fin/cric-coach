/**
 * US-K5 print stylesheet: browser-print path for the net wall. Injected as a
 * <style> element by the reports page (Next.js only allows global CSS files
 * in the root layout, which the timeline story owns). `.no-print` controls
 * (search/create forms, export buttons) disappear on paper; sections avoid
 * page breaks so the "one correction" never splits across sheets.
 */
export const PRINT_STYLES = `
@page { size: A4; margin: 18mm; }
@media print {
  nav, .no-print { display: none !important; }
  body { background: #fff; color: #000; }
  main { margin: 0; padding: 0; max-width: none; }
  .report-view { font-size: 12pt; line-height: 1.45; }
  .report-view section { break-inside: avoid; }
  .report-view a { color: #000; text-decoration: none; }
}
`;
