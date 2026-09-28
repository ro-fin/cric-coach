import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "cricAI — Home Cricket Lab",
  description: "Per-ball analysis, coaching reports and safe workloads",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header>
          <nav aria-label="primary">
            <Link href="/">cricAI</Link> <Link href="/sessions">Sessions</Link>{" "}
            <Link href="/review">Review queue</Link>
          </nav>
        </header>
        {children}
      </body>
    </html>
  );
}
