import { TriangleAlert, RotateCcw } from "lucide-react";
import { Button } from "./Button";

export interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
}

export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-3 rounded-xl border-2 border-danger bg-surface px-5 py-4"
    >
      <p className="flex items-center gap-2 font-semibold text-danger">
        <TriangleAlert aria-hidden="true" className="size-5 shrink-0" />
        {message}
      </p>
      {onRetry && (
        <Button variant="secondary" onClick={onRetry}>
          <RotateCcw aria-hidden="true" className="size-5" />
          Try again
        </Button>
      )}
    </div>
  );
}
