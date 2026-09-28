import { LoaderCircle } from "lucide-react";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant: ButtonVariant;
  size?: ButtonSize;
  /** Busy: disabled, announced as busy, shows a spinner; the label stays. */
  loading?: boolean;
}

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-accent text-accent-ink border-accent hover:opacity-90",
  secondary: "bg-surface text-ink border-border hover:bg-surface-raised",
  ghost: "bg-transparent text-ink border-transparent hover:bg-surface-raised",
  danger: "bg-danger text-white border-danger hover:opacity-90",
};

const SIZES: Record<ButtonSize, string> = {
  md: "min-h-11 px-4 text-base",
  lg: "min-h-14 px-6 text-lg",
};

/** The button look as a class string, for elements that must stay links or labels. */
export function buttonClassName(variant: ButtonVariant, size: ButtonSize = "md", className?: string): string {
  return cn(
    "inline-flex min-w-11 items-center justify-center gap-2 rounded-lg border-2 font-semibold",
    "transition-colors disabled:cursor-not-allowed disabled:opacity-60",
    VARIANTS[variant],
    SIZES[size],
    className,
  );
}

export function Button({
  variant,
  size = "md",
  loading = false,
  disabled,
  type = "button",
  className,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      data-variant={variant}
      className={buttonClassName(variant, size, className)}
      {...rest}
    >
      {loading && <LoaderCircle aria-hidden="true" className="size-5 animate-spin" />}
      {children}
    </button>
  );
}
