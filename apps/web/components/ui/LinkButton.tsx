import Link from "next/link";
import type { ComponentProps } from "react";
import { buttonClassName, type ButtonSize, type ButtonVariant } from "./Button";

export interface LinkButtonProps extends Omit<ComponentProps<typeof Link>, "className"> {
  variant: ButtonVariant;
  size?: ButtonSize;
  className?: string;
}

/** A navigation link that looks like a Button (e.g. "Start session"). It stays a link for assistive tech. */
export function LinkButton({ variant, size = "md", className, ...rest }: LinkButtonProps) {
  return <Link data-variant={variant} className={buttonClassName(variant, size, className)} {...rest} />;
}
