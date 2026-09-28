import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";
import { SessionRoleProvider } from "@/components/shell/SessionRoleProvider";
import { ToastProvider } from "@/components/ui/Toast";
import "./globals.css";

export const metadata: Metadata = {
  title: "cricAI — Home Cricket Lab",
  description: "Per-ball analysis, coaching reports and safe workloads",
  applicationName: "cricAI",
  // Installable on the lab tablet (public/manifest.webmanifest).
  manifest: "/manifest.webmanifest",
  icons: {
    icon: [
      { url: "/icons/icon.svg", type: "image/svg+xml" },
      { url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
    ],
    apple: "/icons/apple-touch-icon.png",
  },
  appleWebApp: { capable: true, title: "cricAI", statusBarStyle: "default" },
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
        <SessionRoleProvider>
          <ToastProvider>
            <AppShell>{children}</AppShell>
          </ToastProvider>
        </SessionRoleProvider>
      </body>
    </html>
  );
}
