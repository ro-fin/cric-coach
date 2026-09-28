/**
 * Zone analytics table (US-K2): per-cell counts, control %, false-shot % and
 * manual/auto provenance straight from the heatmap router — rows render in
 * server order and click through to the cell's ball list. The interactive
 * control is a real <button> in each row's first cell (native keyboard
 * operability + valid ARIA: a tbody's children stay rows), so the table's
 * accessibility tree is intact while the zone label is still Enter/Space
 * operable (WCAG 2.1.1); aria-pressed carries the selected state.
 */

import { formatBallCount } from "@/lib/format";
import type { HeatmapCell } from "./api";
import type { LengthKey, LineKey } from "./geometry";
import type { CellKeyPair } from "./PitchMapSvg";

interface ZoneTableProps {
  cells: HeatmapCell[];
  selectedCell: CellKeyPair | null;
  onSelectCell: (cell: CellKeyPair) => void;
}

function pct(value: number | null): string {
  // Null means no tagged balls landed there: unknowable, shown honestly.
  return value === null ? "—" : `${value}%`;
}

export default function ZoneTable({ cells, selectedCell, onSelectCell }: ZoneTableProps) {
  if (cells.length === 0) {
    return <p data-testid="zone-table-empty">No bounce points on the map yet.</p>;
  }
  return (
    <table data-testid="zone-table">
      <caption>Zone analytics (from the session heatmap)</caption>
      <thead>
        <tr>
          <th scope="col">line</th>
          <th scope="col">length</th>
          <th scope="col">balls</th>
          <th scope="col">control %</th>
          <th scope="col">false shot %</th>
          <th scope="col">manual / auto</th>
          <th scope="col">low-conf</th>
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
            <tr key={`${cell.line}-${cell.length}`} data-testid={`zone-row-${cell.line}-${cell.length}`}>
              <td>
                <button
                  type="button"
                  data-testid={`zone-select-${cell.line}-${cell.length}`}
                  aria-pressed={selected}
                  onClick={select}
                >
                  {cell.line}
                </button>
              </td>
              <td>{cell.length}</td>
              <td>{formatBallCount(cell.balls)}</td>
              <td>{pct(cell.control_pct)}</td>
              <td>{pct(cell.false_shot_pct)}</td>
              <td>{`${cell.sources.manual} / ${cell.sources.auto}`}</td>
              <td>{cell.hollow}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
