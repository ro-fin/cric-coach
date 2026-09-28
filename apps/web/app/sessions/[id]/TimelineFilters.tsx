"use client";

/**
 * Timeline filter bar (US-K1 AC: filters by block, line, length, shot,
 * outcome and control). Options are the pinned tag vocabularies plus the
 * block numbers actually present in this session's tags.
 */

import {
  EMPTY_FILTER,
  LENGTH_OPTIONS,
  LINE_OPTIONS,
  OUTCOME_OPTIONS,
  SHOT_OPTIONS,
  isFilterActive,
} from "./timeline";
import type { TimelineFilter } from "./timeline";

export interface TimelineFiltersProps {
  filter: TimelineFilter;
  blocks: number[];
  onChange: (filter: TimelineFilter) => void;
}

interface SelectSpec {
  key: "line" | "length" | "shot" | "outcome";
  label: string;
  options: readonly string[];
}

const SELECTS: readonly SelectSpec[] = [
  { key: "line", label: "Line", options: LINE_OPTIONS },
  { key: "length", label: "Length", options: LENGTH_OPTIONS },
  { key: "shot", label: "Shot", options: SHOT_OPTIONS },
  { key: "outcome", label: "Outcome", options: OUTCOME_OPTIONS },
];

export default function TimelineFilters({ filter, blocks, onChange }: TimelineFiltersProps) {
  function set(partial: Partial<TimelineFilter>) {
    onChange({ ...filter, ...partial });
  }

  return (
    <fieldset data-testid="timeline-filters">
      <legend>Filters</legend>
      <label>
        Block
        <select
          value={filter.block === null ? "" : String(filter.block)}
          onChange={(event) =>
            set({ block: event.target.value === "" ? null : Number(event.target.value) })
          }
        >
          <option value="">all</option>
          {blocks.map((block) => (
            <option key={block} value={block}>
              {block}
            </option>
          ))}
        </select>
      </label>
      {SELECTS.map(({ key, label, options }) => (
        <label key={key}>
          {label}
          <select
            value={filter[key] ?? ""}
            onChange={(event) =>
              set({
                [key]: event.target.value === "" ? null : event.target.value,
              } as Partial<TimelineFilter>)
            }
          >
            <option value="">all</option>
            {options.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      ))}
      <label>
        Control
        <select
          value={filter.control === null ? "" : String(filter.control)}
          onChange={(event) =>
            set({ control: event.target.value === "" ? null : event.target.value === "true" })
          }
        >
          <option value="">all</option>
          <option value="true">controlled</option>
          <option value="false">uncontrolled</option>
        </select>
      </label>
      <button type="button" disabled={!isFilterActive(filter)} onClick={() => onChange(EMPTY_FILTER)}>
        Clear filters
      </button>
    </fieldset>
  );
}
