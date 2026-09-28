import type { Metadata } from "next";
import { safeNextPath } from "@/lib/auth/next";
import { LoginForm } from "./LoginForm";

export const metadata: Metadata = {
  title: "Sign in — cricAI",
};

interface LoginPageProps {
  searchParams: Promise<{ next?: string | string[] }>;
}

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const { next } = await searchParams;
  return (
    <main className="mx-auto flex max-w-md flex-col gap-6 py-6">
      <div>
        <h1 className="text-3xl font-bold text-ink">Sign in to cricAI</h1>
        <p className="mt-1 text-ink-muted">
          Choose who is using this device and enter that role&apos;s token. You stay signed in on
          this device until you sign out.
        </p>
      </div>
      <LoginForm next={safeNextPath(next)} />
    </main>
  );
}
