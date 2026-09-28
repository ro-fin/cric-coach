/**
 * Zone analytics table (US-K2): per-cell counts, control %, false-shot % and
 * manual/auto provenance straight from the heatmap router — rows render in
 * server order and click through to the cell's ball list. The interactive
 * control is a real <button> in each row's first cell (native keyboard
 * operability + valid ARIA: a tbody's children stay rows), so the table's
 * accessibility tree is intact while the zone label is still Enter/Space
 * operable (WCAG 2.1.1); aria-pressed carries the selected state. Line and
 * length colours come from the line-* and zone-* tokens.
 */

import { Card, CardBody, CardHeader, CardTitle, EmptyState } from "@/components/ui";
import { cn } from "@/lib/cn";
import { formatBallCount } from "@/lib/format";
import type { HeatmapCell } from "./api";
import { type LengthKey, type LineKey, lineTextClass, zoneBgClass } from "./geometry";
import type { CellKeyPair } from "./PitchMapSvg";

interface ZoneTableProps {
  cells: HeatmapCell[];
  selectedCell: CellKeyPair | null;
  onSelectCell: (cell: CellKeyPair) => void;
}

/** Null means no tagged balls landed there: unknowable, said in words. */
export const NO_TAGGED = "no tagged balls";

function pct(value: number | null): string {
  return value === null ? NO_TAGGED : `${value}%`;
}

const TH = "px-3 py-2 text-left text-sm font-semibold text-ink-muted";
const TD = "px-3 py-2 align-middle";

export default function ZoneTable({ cells, selectedCell, onSelectCell }: ZoneTableProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Zones</CardTitle>
      </CardHeader>
      <CardBody>
        {cells.length === 0 ? (
          <div data-testid="zone-table-empty">
            <EmptyState
              title="No bounce points on the map yet"
              description="Zone counts appear once balls have bounce points."
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table data-testid="zone-table" className="w-full border-collapse text-base">
              <caption className="sr-only">Zone analytics (from the session heatmap)</caption>
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className={TH}>line</th>
                  <th scope="col" className={TH}>length</th>
                  <th scope="col" className={TH}>balls</th>
                  <th scope="col" className={TH}>control %</th>
                  <th scope="col" className={TH}>false shot %</th>
                  <th scope="col" className={TH}>manual / auto</th>
                  <th scope="col" className={TH}>low-conf</th>
                </tr>
              </thead>
              <tbody>
                {cells.map((cell) => {
                  const selected =
                    selectedCell !== null &&
                    selectedCell.line === cell.line &&
                    selectedCell.length === cell.length;
                  const select = () =>
                    onSelectCell({ line: cell.line as LineKey, length: cell.length as LengthKey });
                  return (
                    <tr
                      key={`${cell.line}-${cell.length}`}
                      data-testid={`zone-row-${cell.line}-${cell.length}`}
                      className={cn(
                        "border-b border-border last:border-b-0",
                        selected && "bg-surface-raised",
                      )}
                    >
                      <td className={TD}>
                        <button
                          type="button"
                          data-testid={`zone-select-${cell.line}-${cell.length}`}
                          aria-pressed={selected}
                          onClick={select}
                          className={cn(
                            "min-h-11 rounded-lg border-2 px-3 font-semibold",
                            selected ? "border-ink" : "border-transparent hover:border-border",
                            lineTextClass(cell.line),
                          )}
                        >
                          {cell.line}
                        </button>
                      </td>
                      <td className={TD}>
                        <span className="flex items-center gap-2">
                          <span
                            aria-hidden="true"
                            className={cn("size-3 rounded-sm", zoneBgClass(cell.length))}
                          />
                          {cell.length}
                        </span>
                      </td>
                      <td className={TD}>{formatBallCount(cell.balls)}</td>
                      <td className={cn(TD, cell.control_pct === null && "text-ink-muted")}>
                        {pct(cell.control_pct)}
                      </td>
                      <td className={cn(TD, cell.false_shot_pct === null && "text-ink-muted")}>
                        {pct(cell.false_shot_pct)}
                      </td>
                      <td className={TD}>{`${cell.sources.manual} / ${cell.sources.auto}`}</td>
                      <td className={TD}>{cell.hollow}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardBody>
    </Card>
  );
}
