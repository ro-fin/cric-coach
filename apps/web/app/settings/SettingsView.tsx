"use client";

/**
 * App settings (US-J5 review gate, US-L5 live mode), the parent's screen.
 * Versions are append-only: the screen shows the governing version exactly
 * as served (version 0 means the canonical defaults, never saved), the full
 * history, and a form that proposes a whole new version with a reason. The
 * router's approval gates are explained before posting (a review-mode change
 * needs the coach; enabling or widening live mode needs the parent) and the
 * server's 403/409/422 answers are shown verbatim. Other roles are forbidden.
 */

import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  ForbiddenState,
  PageHeader,
  Skeleton,
  useToast,
} from "@/components/ui";
import { type ApiConfig, defaultConfig } from "@/lib/api";
import { useRole } from "@/lib/auth/role";
import { cn } from "@/lib/cn";
import { accessFor, failure, type Load, LOADING, ready } from "../pipeline/access";
import { formatWhen } from "../pipeline/view";
import { type AppSettingsOut, createSettingsApi, REVIEW_MODES, type SettingsApi } from "./api";
import {
  approvalsNeeded,
  approvalText,
  draftFrom,
  draftProblems,
  type SettingsDraft,
  SETTINGS_ROLES,
  toSettingsIn,
} from "./view";

const MODE_LABELS = {
  auto_publish: "Publish reports straight away",
  coach_gate: "Hold reports for the coach to review",
} as const;

const FIELD = "min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-base font-normal text-ink";
const TH = "px-3 py-2 text-left text-sm font-semibold text-ink-muted";
const TD = "px-3 py-2 align-top";

interface SettingsData {
  active: AppSettingsOut;
  versions: AppSettingsOut[];
}

function savedWhen(value: AppSettingsOut): string {
  return value.created_at === null ? "never saved" : formatWhen(value.created_at);
}

function ActiveCard({ active }: { active: AppSettingsOut }) {
  const { report_review: review, live_mode: live } = active.settings;
  return (
    <Card aria-labelledby="settings-active-title">
      <CardHeader>
        <CardTitle id="settings-active-title">{`In force: version ${active.version}`}</CardTitle>
        {active.version === 0 && <Badge tone="info">canonical defaults</Badge>}
      </CardHeader>
      <CardBody className="flex flex-col gap-4">
        <dl className="grid gap-3 sm:grid-cols-2">
          <div>
            <dt className="text-sm font-semibold text-ink-muted">Report review</dt>
            <dd data-testid="active-mode" className="text-ink">
              {`${MODE_LABELS[review.mode] ?? review.mode} (${review.mode})`}
            </dd>
          </div>
          <div>
            <dt className="text-sm font-semibold text-ink-muted">Review deadline</dt>
            <dd data-testid="active-timeout" className="text-ink">{`${review.timeout_hours} hours`}</dd>
          </div>
          <div>
            <dt className="text-sm font-semibold text-ink-muted">Live mode</dt>
            <dd className="text-ink">
              <Badge tone={live.enabled ? "warning" : "neutral"}>{live.enabled ? "on" : "off"}</Badge>
            </dd>
          </div>
          <div>
            <dt className="text-sm font-semibold text-ink-muted">Approved by</dt>
            <dd className="text-ink">{`${active.approved_by}, ${savedWhen(active)}`}</dd>
          </div>
        </dl>
        <div>
          <p className="text-sm font-semibold text-ink-muted">Live keys allowed</p>
          {live.allowlist.length === 0 ? (
            <p className="text-ink-muted">none</p>
          ) : (
            <ul data-testid="active-allowlist" className="mt-1 flex flex-wrap gap-2">
              {live.allowlist.map((key) => (
                <li key={key}>
                  <Badge tone="neutral">{key}</Badge>
                </li>
              ))}
            </ul>
          )}
        </div>
        <p className="text-ink">
          <span className="font-semibold">Reason: </span>
          {active.reason}
        </p>
      </CardBody>
    </Card>
  );
}

function History({ versions }: { versions: AppSettingsOut[] }) {
  return (
    <Card aria-labelledby="settings-history-title">
      <CardHeader>
        <CardTitle id="settings-history-title">Version history</CardTitle>
      </CardHeader>
      <CardBody>
        {versions.length === 0 ? (
          <EmptyState
            title="No saved versions yet"
            description="The canonical defaults are in force until the first version is saved."
          />
        ) : (
          <div className="overflow-x-auto">
            <table data-testid="settings-history" className="w-full border-collapse">
              <caption className="sr-only">Settings versions, oldest first</caption>
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className={TH}>Version</th>
                  <th scope="col" className={TH}>Review</th>
                  <th scope="col" className={TH}>Live</th>
                  <th scope="col" className={TH}>Approved by</th>
                  <th scope="col" className={TH}>Reason</th>
                  <th scope="col" className={TH}>Saved</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((row) => (
                  <tr key={row.version} data-testid={`version-${row.version}`} className="border-b border-border last:border-b-0">
                    <th scope="row" className={cn(TD, "text-left font-semibold text-ink")}>
                      {row.version}
                    </th>
                    <td className={TD}>{row.settings.report_review.mode}</td>
                    <td className={TD}>{row.settings.live_mode.enabled ? "on" : "off"}</td>
                    <td className={TD}>{row.approved_by}</td>
                    <td className={TD}>{row.reason}</td>
                    <td className={TD}>{savedWhen(row)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function NewVersionForm({
  api,
  active,
  onSaved,
}: {
  api: SettingsApi;
  active: AppSettingsOut;
  onSaved: () => void;
}) {
  const role = useRole();
  const { toast } = useToast();
  const [draft, setDraft] = useState<SettingsDraft>(() => draftFrom(active.settings));
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const update = (patch: Partial<SettingsDraft>) => setDraft((current) => ({ ...current, ...patch }));
  const next = toSettingsIn(active.settings, draft);
  const gate = approvalText(approvalsNeeded(active.settings, next.settings), role);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const found = draftProblems(draft);
    setProblems(found);
    if (found.length > 0) {
      return;
    }
    setSaving(true);
    api
      .createVersion(next)
      .then((saved) => {
        toast({ title: `Settings version ${saved.version} saved`, tone: "success" });
        onSaved();
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setProblems([message]);
        toast({ title: "Settings not saved", description: message, tone: "danger" });
      })
      .finally(() => setSaving(false));
  };

  return (
    <Card aria-labelledby="settings-form-title">
      <CardHeader>
        <CardTitle id="settings-form-title">Save a new version</CardTitle>
      </CardHeader>
      <CardBody>
        <form noValidate onSubmit={submit} className="flex flex-col gap-5">
          {problems.length > 0 && (
            <div role="alert" className="rounded-xl border-2 border-danger px-4 py-3">
              <p className="font-semibold text-danger">Not saved:</p>
              <ul className="mt-1 list-disc pl-6 text-ink">
                {problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </div>
          )}
          <fieldset className="flex flex-col gap-2">
            <legend className="font-semibold text-ink">Report review</legend>
            {REVIEW_MODES.map((mode) => (
              <label key={mode} className="flex min-h-11 cursor-pointer items-center gap-3 text-ink">
                <input
                  type="radio"
                  name="review-mode"
                  value={mode}
                  checked={draft.mode === mode}
                  onChange={() => update({ mode })}
                  className="size-5 accent-accent"
                />
                {MODE_LABELS[mode]}
              </label>
            ))}
          </fieldset>
          <label className="flex flex-col gap-2 font-semibold text-ink">
            Review deadline (hours)
            <input
              type="number"
              inputMode="decimal"
              min="0"
              value={draft.timeoutHours}
              onChange={(event) => update({ timeoutHours: event.target.value })}
              className={cn(FIELD, "max-w-40")}
            />
          </label>
          <label className="flex min-h-11 cursor-pointer items-center gap-3 font-semibold text-ink">
            <input
              type="checkbox"
              checked={draft.liveEnabled}
              onChange={(event) => update({ liveEnabled: event.target.checked })}
              className="size-5 accent-accent"
            />
            Live mode on
          </label>
          <label className="flex flex-col gap-2 font-semibold text-ink">
            Live keys allowed (one per line)
            <textarea
              value={draft.allowlist}
              onChange={(event) => update({ allowlist: event.target.value })}
              rows={5}
              className={cn(FIELD, "py-2 font-mono text-sm")}
            />
          </label>
          <label className="flex flex-col gap-2 font-semibold text-ink">
            Reason for this change
            <textarea
              value={draft.reason}
              onChange={(event) => update({ reason: event.target.value })}
              rows={2}
              className={cn(FIELD, "py-2")}
            />
          </label>
          {gate !== null && (
            <p data-testid="approval-note" className="rounded-xl border-2 border-info px-4 py-3 text-ink">
              {gate}
            </p>
          )}
          <div>
            <Button type="submit" variant="primary" loading={saving}>
              Save version
            </Button>
          </div>
        </form>
      </CardBody>
    </Card>
  );
}

export interface SettingsViewProps {
  /** Injected for tests; defaults to the shared lib/api configuration. */
  config?: ApiConfig;
}

export default function SettingsView({ config }: SettingsViewProps) {
  const role = useRole();
  const access = accessFor(role, SETTINGS_ROLES);
  const api = useMemo(() => createSettingsApi(config ?? defaultConfig()), [config]);
  const [data, setData] = useState<Load<SettingsData>>(LOADING);
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    if (access === "forbidden") {
      return;
    }
    let cancelled = false;
    setData(LOADING);
    Promise.all([api.active(), api.versions()]).then(
      ([active, versions]) => {
        if (!cancelled) {
          setData(ready({ active, versions }));
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setData(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [access, api, attempt]);

  const header = (
    <PageHeader
      title="Settings"
      description="How reports are released and what live mode may show. Every change is a new, audited version."
    />
  );

  if (access === "forbidden") {
    return (
      <main className="flex flex-col gap-6">
        {header}
        <ForbiddenState roles={SETTINGS_ROLES} />
      </main>
    );
  }

  return (
    <main className="flex flex-col gap-6">
      {header}
      {data.kind === "loading" && <Skeleton lines={5} label="Loading settings" />}
      {data.kind === "error" && (
        <ErrorState message={`Could not load settings: ${data.message}`} onRetry={reload} />
      )}
      {data.kind === "forbidden" && <ForbiddenState roles={SETTINGS_ROLES} detail={data.message} />}
      {data.kind === "ready" && (
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="flex flex-col gap-6">
            <ActiveCard active={data.data.active} />
            <History versions={data.data.versions} />
          </div>
          <NewVersionForm
            key={data.data.active.version}
            api={api}
            active={data.data.active}
            onSaved={reload}
          />
        </div>
      )}
    </main>
  );
}
