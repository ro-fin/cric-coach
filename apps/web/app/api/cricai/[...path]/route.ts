/** Same-origin API proxy: see lib/server/proxy.ts. */
import { proxy } from "@/lib/server/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ path: string[] }>;
}

async function handle(request: Request, { params }: RouteContext): Promise<Response> {
  const { path } = await params;
  return proxy(request, path);
}

export const GET = handle;
export const HEAD = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
