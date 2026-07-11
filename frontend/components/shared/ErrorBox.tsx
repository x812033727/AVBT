"use client";

import { AlertCircle, RotateCw } from "lucide-react";
import { cn } from "@/lib/utils";

// 全站統一的錯誤框:取代各頁複製的紅色警示框。
export function ErrorBox({
  message,
  onRetry,
  className,
}: {
  message: string;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-center gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300",
        className,
      )}
    >
      <AlertCircle className="h-4 w-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex shrink-0 items-center gap-1 rounded px-2 py-1 text-xs text-red-200 transition hover:bg-red-500/20"
        >
          <RotateCw className="h-3 w-3" aria-hidden />
          重試
        </button>
      ) : null}
    </div>
  );
}
