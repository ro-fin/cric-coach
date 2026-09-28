"use client";

/**
 * The 30-second wellness check-in (US-H4), built for a child on a tablet:
 * large tap targets, an energy scale, sleep hours, soreness by body area and
 * a "something hurts" flag with a note. Out-of-range values are explained in
 * the server's own wording before posting; a 422 from the server is shown
 * verbatim. Any role may check in; the server records who did.
 */

import { Plus, X } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { Button, Card, CardBody, CardHeader, CardTitle, useToast } from "@/components/ui";
import { cn } from "@/lib/cn";
import { ENERGY_MAX, ENERGY_MIN, SORENESS_BODY_KEYS, type WellnessApi } from "./api";
import { WELLNESS_COPY as COPY } from "./copy";
import {
  bodyLabel,
  type CheckinDraft,
  checkinProblems,
  emptyDraft,
  sorenessEntries,
  toCheckinIn,
  todayIso,
} from "./view";

const FIELD = "min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-base text-ink";
const ENERGY_LEVELS = Array.from(
  { length: ENERGY_MAX - ENERGY_MIN + 1 },
  (_, index) => ENERGY_MIN + index,
);

export interface CheckinFormProps {
  api: WellnessApi;
  playerId: string;
  onSaved: () => void;
}

export function CheckinForm({ api, playerId, onSaved }: CheckinFormProps) {
  const { toast } = useToast();
  const ids = useId();
  const [draft, setDraft] = useState<CheckinDraft>(() => emptyDraft(todayIso()));
  const [area, setArea] = useState<string>(SORENESS_BODY_KEYS[0]);
  const [level, setLevel] = useState(1);
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  const update = (patch: Partial<CheckinDraft>) => setDraft((current) => ({ ...current, ...patch }));

  const addSoreness = () =>
    update({ soreness: { ...draft.soreness, [area]: level } });

  const removeSoreness = (key: string) => {
    const next = { ...draft.soreness };
    delete next[key];
    update({ soreness: next });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const found = checkinProblems(draft);
    setProblems(found);
    if (found.length > 0) {
      return;
    }
    setSaving(true);
    api
      .createCheckin(playerId, toCheckinIn(draft))
      .then(() => {
        toast({ title: COPY.saved, tone: "success" });
        setDraft(emptyDraft(todayIso()));
        onSaved();
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setProblems([message]);
        toast({ title: COPY.notSaved, description: message, tone: "danger" });
      })
      .finally(() => setSaving(false));
  };

  const entries = sorenessEntries(draft.soreness);

  return (
    <Card aria-labelledby={`${ids}-title`}>
      <CardHeader>
        <CardTitle id={`${ids}-title`}>{COPY.formTitle}</CardTitle>
      </CardHeader>
      <CardBody>
        <form id="checkin-form" noValidate onSubmit={submit} className="flex flex-col gap-5">
          {problems.length > 0 && (
            <div role="alert" className="rounded-xl border-2 border-danger px-4 py-3">
              <p className="font-semibold text-danger">{COPY.fixFirst}</p>
              <ul className="mt-1 list-disc pl-6 text-ink">
                {problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </div>
          )}

          <label className="flex flex-col gap-2 font-semibold text-ink">
            {COPY.date}
            <input
              type="date"
              value={draft.checkinDate}
              onChange={(event) => update({ checkinDate: event.target.value })}
              className={cn(FIELD, "max-w-xs font-normal")}
            />
          </label>

          <fieldset className="flex flex-col gap-2">
            <legend className="font-semibold text-ink">{COPY.energy}</legend>
            <p className="text-sm text-ink-muted">{COPY.energyHint}</p>
            <div className="flex flex-wrap gap-2">
              {ENERGY_LEVELS.map((value) => {
                const checked = draft.energy === String(value);
                return (
                  <label
                    key={value}
                    className={cn(
                      "flex min-h-11 min-w-14 cursor-pointer items-center justify-center rounded-lg border-2 px-4 text-lg font-semibold",
                      checked ? "border-accent bg-accent text-accent-ink" : "border-border text-ink",
                    )}
                  >
                    <input
                      type="radio"
                      name={`${ids}-energy`}
                      value={value}
                      checked={checked}
                      onChange={() => update({ energy: String(value) })}
                      className="sr-only"
                    />
                    {value}
                  </label>
                );
              })}
            </div>
          </fieldset>

          <label className="flex flex-col gap-2 font-semibold text-ink">
            {COPY.sleep}
            <input
              type="number"
              inputMode="decimal"
              step="0.5"
              min="0"
              value={draft.sleepHours}
              onChange={(event) => update({ sleepHours: event.target.value })}
              className={cn(FIELD, "max-w-40 font-normal")}
            />
          </label>

          <fieldset className="flex flex-col gap-3">
            <legend className="font-semibold text-ink">{COPY.soreness}</legend>
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1 text-sm font-semibold text-ink-muted">
                {COPY.sorenessArea}
                <select
                  value={area}
                  onChange={(event) => setArea(event.target.value)}
                  className={cn(FIELD, "font-normal")}
                >
                  {SORENESS_BODY_KEYS.map((key) => (
                    <option key={key} value={key}>
                      {bodyLabel(key)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-sm font-semibold text-ink-muted">
                {COPY.sorenessLevel}
                <select
                  value={level}
                  onChange={(event) => setLevel(Number(event.target.value))}
                  className={cn(FIELD, "font-normal")}
                >
                  {COPY.sorenessLevels.slice(1).map((label, index) => (
                    <option key={label} value={index + 1}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <Button variant="secondary" onClick={addSoreness}>
                <Plus aria-hidden="true" className="size-5" />
                {COPY.addSoreness}
              </Button>
            </div>
            {entries.length === 0 ? (
              <p className="text-ink-muted">{COPY.noSoreness}</p>
            ) : (
              <ul aria-label={COPY.soreness} className="flex flex-wrap gap-2">
                {entries.map(([key, value]) => (
                  <li
                    key={key}
                    className="flex items-center gap-1 rounded-full border-2 border-border pl-3 text-ink"
                  >
                    {`${bodyLabel(key)}: ${COPY.sorenessLevels[value]}`}
                    <button
                      type="button"
                      onClick={() => removeSoreness(key)}
                      aria-label={`${COPY.removeSoreness} ${bodyLabel(key)}`}
                      className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-full text-ink-muted hover:bg-surface-raised"
                    >
                      <X aria-hidden="true" className="size-5" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </fieldset>

          <label className="flex min-h-11 cursor-pointer items-center gap-3 text-lg font-semibold text-ink">
            <input
              type="checkbox"
              checked={draft.pain}
              onChange={(event) => update({ pain: event.target.checked })}
              className="size-6 accent-danger"
            />
            {COPY.pain}
          </label>
          {draft.pain && (
            <label className="flex flex-col gap-2 font-semibold text-ink">
              {COPY.painNote}
              <textarea
                value={draft.painNote}
                onChange={(event) => update({ painNote: event.target.value })}
                rows={3}
                className={cn(FIELD, "py-2 font-normal")}
              />
            </label>
          )}

          <div>
            <Button type="submit" variant="primary" size="lg" loading={saving}>
              {COPY.submit}
            </Button>
          </div>
        </form>
      </CardBody>
    </Card>
  );
}
