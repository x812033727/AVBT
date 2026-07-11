"use client";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";
import type { TrackedKind, TrackerStatus } from "@/lib/api";

const KIND_FILTERS: { value: TrackedKind | ""; label: string }[] = [
  { value: "", label: "全部" },
  { value: "star", label: "女優" },
  { value: "studio", label: "製作商" },
  { value: "label", label: "發行商" },
  { value: "series", label: "系列" },
  { value: "director", label: "導演" },
];

// 追蹤頁頂列:標題 + 分類篩選 + 背景掃描開關 + 批次動作。
// 純展示元件:filter / trackerStatus 等 state 與切換邏輯都在 page 層。
export default function TrackedToolbar({
  filteredCount,
  totalCount,
  filter,
  onFilterChange,
  trackerStatus,
  onToggleBackgroundScan,
  missingLoading,
  anyChecking,
  batchActive,
  onOpenMissingSummary,
  onOpenCheckAll,
}: {
  filteredCount: number;
  totalCount: number;
  filter: TrackedKind | "";
  onFilterChange: (value: TrackedKind | "") => void;
  trackerStatus: TrackerStatus | null;
  onToggleBackgroundScan: (enabled: boolean) => void;
  missingLoading: boolean;
  anyChecking: boolean;
  batchActive: boolean;
  onOpenMissingSummary: () => void;
  onOpenCheckAll: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <h1 className="text-lg font-semibold">追蹤中</h1>
      <span className="text-sm text-muted-foreground">
        ({filteredCount} / {totalCount})
      </span>
      <div className="flex flex-wrap gap-1">
        {KIND_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => onFilterChange(f.value)}
            className={cn(
              "rounded-md px-3 py-1 text-xs transition",
              filter === f.value
                ? "bg-primary text-primary-foreground"
                : "border border-border text-muted-foreground hover:bg-muted hover:text-foreground"
            )}
          >
            {f.label}
          </button>
        ))}
      </div>
      <div className="ml-auto flex items-center gap-3">
        <label
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
          title={
            trackerStatus
              ? `開啟後每 ${trackerStatus.interval_seconds} 秒自動檢查所有追蹤項目的新作品 / 缺漏；關閉只停掉排程，手動「立即檢查」仍可用`
              : "背景排程掃描"
          }
        >
          <Checkbox
            checked={trackerStatus?.enabled ?? false}
            onCheckedChange={(v) => onToggleBackgroundScan(v === true)}
            disabled={!trackerStatus}
          />
          背景掃描
        </label>
        {filteredCount > 0 && (
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onOpenMissingSummary}
              disabled={missingLoading || batchActive}
              title="重新掃 PikPak 資料夾並重算缺漏"
            >
              重算缺漏
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onOpenCheckAll}
              disabled={anyChecking || batchActive}
            >
              全部立即檢查
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
