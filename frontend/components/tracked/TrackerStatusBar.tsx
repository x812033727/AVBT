"use client";

import { Progress } from "@/components/ui/progress";
import type { TrackerStatus } from "@/lib/api";

// 背景掃描進行中的行內橫幅:顯示 run_loop 的 X/Y 進度。純展示元件,
// 輪詢與 status state 都留在 page 層。
export default function TrackerStatusBar({ status }: { status: TrackerStatus }) {
  const percent = Math.round(
    (status.scan_current / Math.max(status.scan_total, 1)) * 100
  );
  return (
    <div className="space-y-2 rounded-md border border-blue-500/30 bg-blue-500/10 px-3 py-2 text-sm text-blue-200">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>
          背景掃描中…{" "}
          {status.scan_total > 0 && (
            <span className="font-mono">
              {status.scan_current} / {status.scan_total} ({percent}%)
            </span>
          )}
        </span>
        {status.scan_name && (
          <span className="truncate font-mono text-blue-300/80">
            {status.scan_name}
          </span>
        )}
      </div>
      {status.scan_total > 0 && <Progress value={percent} className="h-1.5" />}
    </div>
  );
}
