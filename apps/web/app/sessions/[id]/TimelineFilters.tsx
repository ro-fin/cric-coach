"use client";

/**
 * Timeline filter bar (US-K1 AC: filters by block, line, length, shot,
 * outcome and control). Options are the pinned tag vocabularies plus the
 * block numbers actually present in this session's tags.
 */

import { useId } from "react";
import { Button } from "@/components/ui";
import {
  EMPTY_FILTER,
  LENGTH_OPTIONS,
  LINE_OPTIONS,
  OUTCOME_OPTIONS,
  SHOT_OPTIONS,
  isFilterActive,
} from "./timeline";
import type { TimelineFilter } from "./timeline";

const FIELD =
  "min-h-11 w-full rounded-lg border-2 border-border bg-surface px-2 text-ink focus:border-accent";
const LABEL = "flex flex-col gap-1 text-sm font-semibold";

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
  // Each <select> is named by a sibling <label htmlFor>, never by a wrapping label:
  // a wrapping label would pull every option text into the accessible name.
  const baseId = useId();
  const idFor = (key: string) => `${baseId}-${key}`;

  function set(partial: Partial<TimelineFilter>) {
    onChange({ ...filter, ...partial });
  }

  return (
    <fieldset data-testid="timeline-filters" className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      <legend className="mb-2 text-lg font-semibold">Filters</legend>
      <div className={LABEL}>
        <label htmlFor={idFor("block")}>Block</label>
        <select
          id={idFor("block")}
          className={FIELD}
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
      </div>
      {SELECTS.map(({ key, label, options }) => (
        <div key={key} className={LABEL}>
          <label htmlFor={idFor(key)}>{label}</label>
          <select
            id={idFor(key)}
            className={FIELD}
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
        </div>
      ))}
      <div className={LABEL}>
        <label htmlFor={idFor("control")}>Control</label>
        <select
          id={idFor("control")}
          className={FIELD}
          value={filter.control === null ? "" : String(filter.control)}
          onChange={(event) =>
            set({ control: event.target.value === "" ? null : event.target.value === "true" })
          }
        >
          <option value="">all</option>
          <option value="true">controlled</option>
          <option value="false">uncontrolled</option>
        </select>
      </div>
      <Button
        variant="ghost"
        className="self-end"
        disabled={!isFilterActive(filter)}
        onClick={() => onChange(EMPTY_FILTER)}
      >
        Clear filters
      </Button>
    </fieldset>
  );
}
