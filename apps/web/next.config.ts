import type { NextConfig } from "next";
import { fileURLToPath } from "url";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Self-contained server bundle (.next/standalone) for the production image
  // (deploy/Dockerfile.web, target "runtime", sets CRICAI_WEB_STANDALONE=1).
  // Opt-in because tracing a pnpm tree creates symlinks, which Windows
  // refuses without Developer Mode; `next start` builds stay the default.
  output: process.env.CRICAI_WEB_STANDALONE === "1" ? "standalone" : undefined,
  // Trace from the monorepo root so pnpm's workspace links resolve.
  outputFileTracingRoot: fileURLToPath(new URL("../..", import.meta.url)),
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
