"use client";

import { LogIn } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/Button";
import { ROLE_LABELS, ROLES, type Role } from "@/lib/auth/roles";
import { hardNavigate } from "@/lib/browser";
import { cn } from "@/lib/cn";

const ROLE_HINTS: Record<Role, string> = {
  player: "Your sessions, progress and today's goal",
  parent: "Everything, plus cameras and settings",
  coach: "Review queue, notes and reports",
};

export interface LoginFormProps {
  /** Same-origin path to open after signing in. */
  next: string;
}

async function detailOf(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // not JSON: fall through
  }
  return `Sign-in failed (HTTP ${response.status}).`;
}

export function LoginForm({ next }: LoginFormProps) {
  const [role, setRole] = useState<Role | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (role === null || token.trim() === "") {
      setError("Choose a role and enter its token.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/cricai/_session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role, token }),
      });
      if (response.ok) {
        hardNavigate(next);
        return;
      }
      setError(await detailOf(response));
    } catch {
      setError("This device could not reach the dashboard server.");
    }
    setBusy(false);
  };

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-6">
      <fieldset className="flex flex-col gap-3">
        <legend className="mb-2 text-lg font-semibold text-ink">Who is using cricAI?</legend>
        {ROLES.map((option) => (
          <label
            key={option}
            className={cn(
              "flex min-h-11 cursor-pointer items-start gap-3 rounded-xl border-2 bg-surface px-4 py-3",
              role === option ? "border-accent" : "border-border",
            )}
          >
            <input
              type="radio"
              name="role"
              value={option}
              checked={role === option}
              onChange={() => setRole(option)}
              className="mt-1 accent-accent"
            />
            <span>
              <span className="block font-semibold text-ink">{ROLE_LABELS[option]}</span>
              <span className="block text-sm text-ink-muted">{ROLE_HINTS[option]}</span>
            </span>
          </label>
        ))}
      </fieldset>

      <div className="flex flex-col gap-2">
        <label htmlFor="token" className="font-semibold text-ink">
          Role token
        </label>
        <input
          id="token"
          name="token"
          type="password"
          autoComplete="current-password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          className="min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-ink"
        />
      </div>

      {error && (
        <p role="alert" className="rounded-lg border-2 border-danger bg-surface px-4 py-3 font-semibold text-danger">
          {error}
        </p>
      )}

      <Button type="submit" variant="primary" size="lg" loading={busy}>
        <LogIn aria-hidden="true" className="size-5" />
        Sign in
      </Button>
    </form>
  );
}
