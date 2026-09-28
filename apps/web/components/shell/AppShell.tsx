"use client";

import { Ellipsis, UserRound } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { Dialog } from "@/components/ui/Dialog";
import { useRole } from "@/lib/auth/role";
import { ROLE_LABELS } from "@/lib/auth/roles";
import { cn } from "@/lib/cn";
import { isActive, navItemsFor, type NavItem } from "./nav";
import { ThemeToggle } from "./ThemeToggle";

/** How many destinations fit in the tablet tab bar before the rest move under "More". */
export const TAB_BAR_SLOTS = 4;

function NavLink({
  item,
  pathname,
  compact = false,
  onNavigate,
}: {
  item: NavItem;
  pathname: string;
  compact?: boolean;
  onNavigate?: () => void;
}) {
  const active = isActive(item.href, pathname);
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      className={cn(
        "flex min-h-11 items-center rounded-lg font-semibold",
        compact ? "flex-1 flex-col justify-center gap-0.5 px-1 text-sm" : "gap-3 px-3",
        active ? "bg-accent text-accent-ink" : "text-ink hover:bg-surface-raised",
      )}
    >
      <Icon aria-hidden="true" className={compact ? "size-6" : "size-5"} />
      {item.label}
    </Link>
  );
}

function RoleChip() {
  const role = useRole();
  return (
    <p className="flex items-center gap-2 text-sm text-ink-muted">
      <UserRound aria-hidden="true" className="size-5" />
      {role === null ? "Not signed in" : `Signed in as ${ROLE_LABELS[role]}`}
    </p>
  );
}

/**
 * The app frame around every page: a sidebar at 1024px and wider, a bottom
 * tab bar below that, the theme toggle and the signed-in role. Pages render
 * their own <main>; the shell adds no landmark of that kind.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const role = useRole();
  const [moreOpen, setMoreOpen] = useState(false);
  const items = navItemsFor(role);
  // A "More" button only earns its slot when it hides two or more destinations.
  const overflow = items.length > TAB_BAR_SLOTS + 1;
  const tabItems = overflow ? items.slice(0, TAB_BAR_SLOTS) : items;
  const moreItems = overflow ? items.slice(TAB_BAR_SLOTS) : [];
  const moreActive = moreItems.some((item) => isActive(item.href, pathname));

  return (
    <div className="min-h-screen bg-bg text-ink lg:flex">
      <a
        href="#content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-50 focus:rounded-lg focus:bg-surface focus:p-3"
      >
        Skip to content
      </a>

      <aside
        data-print="hide"
        className="hidden border-r border-border bg-surface lg:sticky lg:top-0 lg:flex lg:h-screen lg:w-64 lg:shrink-0 lg:flex-col lg:gap-4 lg:p-4"
      >
        <Link href="/" className="flex min-h-11 items-center px-3 text-2xl font-bold text-ink">
          cricAI
        </Link>
        <nav aria-label="Primary" className="flex flex-1 flex-col gap-1 overflow-y-auto">
          {items.map((item) => (
            <NavLink key={item.href} item={item} pathname={pathname} />
          ))}
        </nav>
        <div className="flex flex-col gap-2 border-t border-border pt-4">
          <RoleChip />
          <ThemeToggle />
        </div>
      </aside>

      <header
        data-print="hide"
        className="sticky top-0 z-30 flex items-center justify-between gap-2 border-b border-border bg-surface px-4 lg:hidden"
      >
        <Link href="/" className="flex min-h-11 items-center text-xl font-bold text-ink">
          cricAI
        </Link>
        <div className="flex items-center gap-2">
          <RoleChip />
          <ThemeToggle />
        </div>
      </header>

      <div id="content" tabIndex={-1} className="flex-1 px-4 pb-28 pt-6 lg:px-8 lg:pb-10">
        <div className="mx-auto max-w-6xl">{children}</div>
      </div>

      <nav
        aria-label="Tabs"
        data-print="hide"
        className="fixed inset-x-0 bottom-0 z-40 flex gap-1 border-t border-border bg-surface px-2 pb-[env(safe-area-inset-bottom)] pt-1 lg:hidden"
      >
        {tabItems.map((item) => (
          <NavLink key={item.href} item={item} pathname={pathname} compact />
        ))}
        {moreItems.length > 0 && (
          <button
            type="button"
            aria-haspopup="dialog"
            onClick={() => setMoreOpen(true)}
            className={cn(
              "flex min-h-11 flex-1 flex-col items-center justify-center gap-0.5 rounded-lg px-1 text-sm font-semibold",
              moreActive ? "bg-accent text-accent-ink" : "text-ink hover:bg-surface-raised",
            )}
          >
            <Ellipsis aria-hidden="true" className="size-6" />
            More
          </button>
        )}
      </nav>

      <Dialog open={moreOpen} onOpenChange={setMoreOpen} title="More">
        <nav aria-label="More" className="flex flex-col gap-1">
          {moreItems.map((item) => (
            <NavLink
              key={item.href}
              item={item}
              pathname={pathname}
              onNavigate={() => setMoreOpen(false)}
            />
          ))}
        </nav>
      </Dialog>
    </div>
  );
}
