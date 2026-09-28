"use client";

/**
 * New session (US-A3, US-A5, US-B1): the lab operator's guided flow from
 * "who is batting" to "recording stopped". Details create the session; a
 * machine session then passes the parent-acknowledged safety checklist; the
 * operator picks cameras and starts recording; stopping reports any failed
 * camera and shows the server's capture verdict verbatim.
 *
 * The session id is written to the URL (`?session=<id>`) as soon as it
 * exists, so a tablet reload resumes at the step the server's lifecycle state
 * implies instead of creating a duplicate session.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  LinkButton,
  PageHeader,
  Skeleton,
} from "@/components/ui";
import type { SessionOut } from "@/lib/api";
import type { Role } from "@/lib/auth/roles";
import { describeError } from "@/app/_today/useLoad";
import { createNewSessionApi } from "./api";
import type { NewSessionApi, StartOut, StopOut } from "./api";
import {
  STEP_LABELS,
  afterCreate,
  emptyDetails,
  localIsoDate,
  resumeStep,
  stepsFor,
} from "./flow";
import type { DetailsForm, Step } from "./flow";
import { CamerasStep, ChecklistStep, DetailsStep, DoneStep, RecordingStep } from "./steps";

export interface NewSessionFlowProps {
  role: Role | null;
  search: string;
  api?: NewSessionApi;
  /** Injectable clock for the default session date. */
  now?: () => Date;
}

interface Verdict {
  state: string;
  degraded: boolean;
  missingViews: string[];
}

type Resume =
  | { status: "none" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready" };

function writeSessionToUrl(sessionId: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("session", sessionId);
  window.history.replaceState(null, "", url.toString());
}

export default function NewSessionFlow({
  role,
  search,
  api: injected,
  now = () => new Date(),
}: NewSessionFlowProps) {
  const api = useMemo(() => injected ?? createNewSessionApi(), [injected]);
  const params = new URLSearchParams(search);
  const resumeId = params.get("session");
  const [form, setForm] = useState<DetailsForm>(() =>
    emptyDetails(params.get("player") ?? "", localIsoDate(now())),
  );
  const [step, setStep] = useState<Step>("details");
  const [session, setSession] = useState<SessionOut | null>(null);
  const [start, setStart] = useState<StartOut | null>(null);
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [resume, setResume] = useState<Resume>({ status: resumeId === null ? "none" : "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (resumeId === null) return;
    let current = true;
    setResume({ status: "loading" });
    Promise.all([api.getSession(resumeId), api.lifecycle(resumeId)]).then(
      ([found, lifecycle]) => {
        if (!current) return;
        setSession(found);
        setStep(resumeStep(found, lifecycle.state));
        setVerdict({
          state: lifecycle.state,
          degraded: lifecycle.degraded,
          missingViews: lifecycle.missing_views,
        });
        setResume({ status: "ready" });
      },
      (error: unknown) => {
        if (current) setResume({ status: "error", message: describeError(error).message });
      },
    );
    return () => {
      current = false;
    };
  }, [api, resumeId, attempt]);

  const allowed = role === "parent" || role === "coach";
  const bowler = session === null ? form.bowlerSource : session.bowler_source;
  const steps = stepsFor(bowler);

  function created(next: SessionOut) {
    setSession(next);
    writeSessionToUrl(next.id);
    setStep(afterCreate(next.bowler_source));
  }

  function started(next: StartOut) {
    setStart(next);
    setStep("recording");
  }

  function stopped(next: StopOut) {
    setVerdict({ state: next.state, degraded: next.degraded, missingViews: next.missing_views });
    setStep("done");
  }

  let body;
  if (!allowed) {
    body = (
      <EmptyState
        title="Only a parent or coach can start a session"
        action={
          <LinkButton variant="secondary" href="/sessions">
            See sessions
          </LinkButton>
        }
      />
    );
  } else if (resume.status === "loading") {
    body = <Skeleton label="Loading the session" />;
  } else if (resume.status === "error") {
    body = (
      <ErrorState
        message={`Could not resume the session: ${resume.message}`}
        onRetry={() => setAttempt((n) => n + 1)}
      />
    );
  } else if (step === "details" || session === null) {
    body = <DetailsStep api={api} form={form} onChange={setForm} onCreated={created} />;
  } else if (step === "checklist") {
    body = (
      <ChecklistStep api={api} sessionId={session.id} onAcked={() => setStep("cameras")} />
    );
  } else if (step === "cameras") {
    body = (
      <CamerasStep
        api={api}
        sessionId={session.id}
        onStarted={started}
        onBackToChecklist={
          session.bowler_source === "machine" ? () => setStep("checklist") : null
        }
      />
    );
  } else if (step === "recording") {
    body = <RecordingStep api={api} sessionId={session.id} start={start} onStopped={stopped} />;
  } else {
    // "done" is only ever reached with the stop or lifecycle verdict in hand
    const done = verdict!;
    body = (
      <DoneStep
        sessionId={session.id}
        state={done.state}
        degraded={done.degraded}
        missingViews={done.missingViews}
      />
    );
  }

  return (
    <main className="mx-auto w-full max-w-4xl p-4 md:p-6">
      <PageHeader
        title="New session"
        description="Set up, run the safety check, record, stop."
      />
      {allowed && (
        <ol aria-label="Steps" className="mb-6 flex flex-wrap gap-2">
          {steps.map((entry, index) => (
            <li
              key={entry.id}
              aria-current={entry.id === step ? "step" : undefined}
              className={`rounded-full border-2 px-3 py-1 text-sm font-semibold ${
                entry.id === step
                  ? "border-accent bg-accent text-accent-ink"
                  : "border-border text-ink-muted"
              }`}
            >
              {index + 1}. {entry.label}
            </li>
          ))}
        </ol>
      )}
      <Card aria-labelledby="new-session-step">
        <CardHeader>
          <CardTitle id="new-session-step">{allowed ? STEP_LABELS[step] : "New session"}</CardTitle>
        </CardHeader>
        <CardBody>{body}</CardBody>
      </Card>
    </main>
  );
}
