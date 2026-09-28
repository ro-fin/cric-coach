/**
 * Every dashboard route and who may see it (team contract section 3.5). T3
 * and T4 never edit this file: all routes are registered up front.
 */
import {
  Bell,
  Camera,
  ClipboardCheck,
  FileText,
  HeartPulse,
  House,
  NotebookPen,
  Settings,
  TrendingUp,
  Video,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { ROLES, type Role } from "@/lib/auth/roles";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  roles: Role[];
}

const ALL: Role[] = [...ROLES];

/** Top-level destinations, in display order. The first four a role can see go in the tablet tab bar. */
export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Today", icon: House, roles: ALL },
  { href: "/sessions", label: "Sessions", icon: Video, roles: ALL },
  { href: "/progress", label: "Progress", icon: TrendingUp, roles: ALL },
  { href: "/reports", label: "Reports", icon: FileText, roles: ALL },
  { href: "/wellness", label: "Wellness", icon: HeartPulse, roles: ALL },
  { href: "/review", label: "Review queue", icon: ClipboardCheck, roles: ["coach"] },
  { href: "/notes", label: "Notes", icon: NotebookPen, roles: ["coach", "parent"] },
  { href: "/pipeline", label: "Pipeline", icon: Workflow, roles: ["parent", "coach"] },
  { href: "/alerts", label: "Alerts", icon: Bell, roles: ["parent", "coach"] },
  { href: "/cameras", label: "Cameras", icon: Camera, roles: ["parent"] },
  { href: "/settings", label: "Settings", icon: Settings, roles: ["parent"] },
];

export interface RouteAccess {
  /** Path pattern; `[param]` matches one segment. */
  pattern: string;
  /** Roles allowed, or "public" for pages that need no sign-in. */
  roles: Role[] | "public";
}

/** The full route table, including pages that are not nav destinations. Most specific first. */
export const ROUTES: RouteAccess[] = [
  { pattern: "/login", roles: "public" },
  { pattern: "/sessions/new", roles: ["parent", "coach"] },
  { pattern: "/sessions/[id]", roles: ALL },
  { pattern: "/pitchmap/[sessionId]", roles: ALL },
  ...NAV_ITEMS.map((item) => ({ pattern: item.href, roles: item.roles })),
];

function matches(pattern: string, pathname: string): boolean {
  const want = pattern.split("/");
  const have = pathname.replace(/\/+$/, "").split("/");
  if (pattern === "/") {
    return pathname === "/";
  }
  return (
    want.length === have.length &&
    want.every((part, index) => part.startsWith("[") || part === have[index])
  );
}

/** Access rule for a pathname, or undefined for a path outside the route table. */
export function routeFor(pathname: string): RouteAccess | undefined {
  return ROUTES.find((route) => matches(route.pattern, pathname));
}

/** Nav destinations the role may see; everything when the role is unknown. */
export function navItemsFor(role: Role | null): NavItem[] {
  return role === null ? NAV_ITEMS : NAV_ITEMS.filter((item) => item.roles.includes(role));
}

/** Whether a nav destination is the current page (sections own their sub-pages). */
export function isActive(href: string, pathname: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}
