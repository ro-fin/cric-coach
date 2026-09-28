import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";
import { ToastProvider } from "@/components/ui/Toast";
import { RoleProvider } from "@/lib/auth/role";
import "./globals.css";

export const metadata: Metadata = {
  title: "cricAI — Home Cricket Lab",
  description: "Per-ball analysis, coaching reports and safe workloads",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f5f7f3" },
    { media: "(prefers-color-scheme: dark)", color: "#0e1411" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // The theme toggle sets data-theme on <html> after hydration.
    <html lang="en" suppressHydrationWarning>
      <body>
        <RoleProvider role={null}>
          <ToastProvider>
            <AppShell>{children}</AppShell>
          </ToastProvider>
        </RoleProvider>
      </body>
    </html>
  );
}
