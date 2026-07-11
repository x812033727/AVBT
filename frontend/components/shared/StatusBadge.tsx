import { cn } from "@/lib/utils";
import type { StatusTone } from "@/lib/status";

// 全站統一的狀態徽章:半透明底 + 亮字,對應 lib/status.ts 的六檔語意。
const TONE_CLASSES: Record<StatusTone, string> = {
  neutral: "bg-white/10 text-white/70",
  info: "bg-blue-500/20 text-blue-300",
  success: "bg-emerald-500/20 text-emerald-300",
  warning: "bg-amber-500/20 text-amber-300",
  danger: "bg-red-500/20 text-red-300",
  muted: "bg-white/5 text-white/40",
};

export function StatusBadge({
  tone,
  className,
  children,
}: {
  tone: StatusTone;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium",
        TONE_CLASSES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
