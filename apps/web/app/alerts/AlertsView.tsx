"use client";

/**
 * Alerts inbox (US-L4 dashboard half). Routing is authorization on the
 * server: the parent sees parent-audience alerts (device health, honesty
 * notices), the coach holds the developer seat (pipeline, model drift,
 * canary). The open inbox is the default; acknowledged alerts can be shown
 * too. Acknowledging is idempotent on the server and the row is replaced by
 * the server's answer. Players see the forbidden state and no request is made.
 */

import { Check } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
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
import { accessFor, failure, type Load, LOADING, ready } from "../pipeline/access";
import { formatWhen } from "../pipeline/view";
import { type AlertOut, createAlertsApi } from "./api";
import { ALERT_ROLES, audienceText, detailRows, replaceAlert, severityTone } from "./view";

const LINK = "inline-flex min-h-11 items-center font-semibold text-accent underline underline-offset-4";

function AlertCard({
  alert,
  busy,
  onAcknowledge,
}: {
  alert: AlertOut;
  busy: boolean;
  onAcknowledge: (id: string) => void;
}) {
  const titleId = `alert-${alert.id}-title`;
  const rows = detailRows(alert.detail);
  return (
    <li data-testid={`alert-${alert.id}`}>
      <Card aria-labelledby={titleId}>
        <CardHeader>
          <CardTitle id={titleId} className="font-mono text-lg">
            {alert.code}
          </CardTitle>
          <div className="flex flex-wrap gap-2">
            <Badge tone={severityTone(alert.severity)}>{alert.severity}</Badge>
            <Badge tone="neutral">{alert.audience}</Badge>
            {alert.acknowledged && <Badge tone="success">acknowledged</Badge>}
          </div>
        </CardHeader>
        <CardBody className="flex flex-col gap-3">
          <p className="text-sm text-ink-muted">
            <time dateTime={alert.created_at}>{formatWhen(alert.created_at)}</time>
          </p>
          {rows.length > 0 && (
            <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-[max-content_1fr]">
              {rows.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="font-semibold text-ink-muted">{key}</dt>
                  <dd className="text-ink">{value}</dd>
                </div>
              ))}
            </dl>
          )}
          <div className="flex flex-wrap items-center gap-4">
            {alert.session_id !== null && (
              <>
                <Link href={`/sessions/${alert.session_id}`} className={LINK}>
                  Open session
                </Link>
                <Link href={`/pipeline?session=${alert.session_id}`} className={LINK}>
                  Pipeline runs
                </Link>
              </>
            )}
            {!alert.acknowledged && (
              <Button variant="secondary" loading={busy} onClick={() => onAcknowledge(alert.id)}>
                <Check aria-hidden="true" className="size-5" />
                Acknowledge
              </Button>
            )}
          </div>
        </CardBody>
      </Card>
    </li>
  );
}

export interface AlertsViewProps {
  /** Injected for tests; defaults to the shared lib/api configuration. */
  config?: ApiConfig;
}

export default function AlertsView({ config }: AlertsViewProps) {
  const role = useRole();
  const access = accessFor(role, ALERT_ROLES);
  const { toast } = useToast();
  const api = useMemo(() => createAlertsApi(config ?? defaultConfig()), [config]);
  const [showAcknowledged, setShowAcknowledged] = useState(false);
  const [alerts, setAlerts] = useState<Load<AlertOut[]>>(LOADING);
  const [attempt, setAttempt] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    if (access === "forbidden") {
      return;
    }
    let cancelled = false;
    setAlerts(LOADING);
    api.listAlerts(showAcknowledged ? {} : { acknowledged: false }).then(
      (rows) => {
        if (!cancelled) {
          setAlerts(ready(rows));
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setAlerts(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [access, api, showAcknowledged, attempt]);

  const acknowledge = (list: AlertOut[], id: string) => {
    setBusyId(id);
    api
      .acknowledge(id)
      .then((updated) => {
        setAlerts(ready(replaceAlert(list, updated)));
        toast({ title: "Alert acknowledged", description: updated.code, tone: "success" });
      })
      .catch((error: unknown) => {
        toast({
          title: "Alert not acknowledged",
          description: error instanceof Error ? error.message : String(error),
          tone: "danger",
        });
      })
      .finally(() => setBusyId(null));
  };

  const header = <PageHeader title="Alerts" description={audienceText(role)} />;

  if (access === "forbidden") {
    return (
      <main className="flex flex-col gap-6">
        {header}
        <ForbiddenState roles={ALERT_ROLES} />
      </main>
    );
  }

  return (
    <main className="flex flex-col gap-6">
      {header}
      <label className="flex min-h-11 cursor-pointer items-center gap-3 text-ink">
        <input
          type="checkbox"
          checked={showAcknowledged}
          onChange={(event) => setShowAcknowledged(event.target.checked)}
          className="size-5 accent-accent"
        />
        Show acknowledged alerts too
      </label>
      {alerts.kind === "loading" && <Skeleton lines={4} label="Loading alerts" />}
      {alerts.kind === "error" && (
        <ErrorState
          message={`Could not load alerts: ${alerts.message}`}
          onRetry={() => setAttempt((value) => value + 1)}
        />
      )}
      {alerts.kind === "forbidden" && <ForbiddenState roles={ALERT_ROLES} detail={alerts.message} />}
      {alerts.kind === "ready" && alerts.data.length === 0 && (
        <EmptyState
          title={showAcknowledged ? "No alerts" : "No open alerts"}
          description={
            showAcknowledged
              ? "Nothing has been routed to your role yet."
              : "Nothing routed to your role is waiting for attention."
          }
        />
      )}
      {alerts.kind === "ready" && alerts.data.length > 0 && (
        <ul aria-label="alerts, newest first" className="flex flex-col gap-4">
          {alerts.data.map((alert) => (
            <AlertCard
              key={alert.id}
              alert={alert}
              busy={busyId === alert.id}
              onAcknowledge={(id) => acknowledge(alerts.data, id)}
            />
          ))}
        </ul>
      )}
    </main>
  );
}
