"use client";

import { Radar } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import {
  TRACKED_LABELS,
  type MissingCodesResult,
  type MissingSummaryItem,
  type PresenceCodeLookup,
  type TrackedKind,
  type TrackedListing,
} from "@/lib/api";
import TrackedRow, { keyOf } from "./TrackedRow";

// 追蹤清單:空狀態 + 逐列 TrackedRow。所有跨列共享的 Map/Set 都由
// page 層持有,這裡只負責「按 key 取值後分發到每一列」。
export default function TrackedList({
  items,
  filter,
  missing,
  missingLoading,
  expanded,
  details,
  detailLoading,
  checkingKey,
  checkingPhase,
  batchActive,
  lookups,
  lookupBusy,
  onCheckNow,
  onToggleExpand,
  onLookup,
  onChanged,
}: {
  items: TrackedListing[];
  filter: TrackedKind | "";
  missing: Map<string, MissingSummaryItem> | null;
  missingLoading: boolean;
  expanded: Set<string>;
  details: Map<string, MissingCodesResult>;
  detailLoading: Set<string>;
  checkingKey: string | null;
  checkingPhase: string;
  batchActive: boolean;
  lookups: Map<string, PresenceCodeLookup>;
  lookupBusy: Set<string>;
  onCheckNow: (it: TrackedListing) => void;
  onToggleExpand: (it: TrackedListing) => void;
  onLookup: (code: string) => void;
  onChanged: () => void;
}) {
  if (!items.length) {
    if (filter) {
      return (
        <EmptyState
          icon={Radar}
          title={`沒有追蹤任何${TRACKED_LABELS[filter as TrackedKind]}`}
        />
      );
    }
    return (
      <EmptyState
        icon={Radar}
        title="還沒追蹤任何東西"
        hint="可在上方手動新增,或到對應頁面點「★ 追蹤」:"
        action={
          <div className="flex flex-wrap justify-center gap-2 text-xs">
            <code className="rounded bg-muted px-2 py-0.5">/star/{"{slug}"}</code>
            <code className="rounded bg-muted px-2 py-0.5">/studio/{"{slug}"}</code>
            <code className="rounded bg-muted px-2 py-0.5">/series/{"{slug}"}</code>
            <code className="rounded bg-muted px-2 py-0.5">/label/{"{slug}"}</code>
            <code className="rounded bg-muted px-2 py-0.5">/director/{"{slug}"}</code>
          </div>
        }
      />
    );
  }

  return (
    <div className="grid gap-3">
      {items.map((it) => {
        const key = keyOf(it);
        return (
          <TrackedRow
            key={key}
            item={it}
            missing={missing?.get(key)}
            missingLoading={missingLoading}
            expanded={expanded.has(key)}
            isChecking={checkingKey === key}
            checkingPhase={checkingPhase}
            checkDisabled={!!checkingKey || batchActive}
            batchActive={batchActive}
            detail={details.get(key) || null}
            detailLoading={detailLoading.has(key)}
            lookups={lookups}
            lookupBusy={lookupBusy}
            onCheckNow={() => onCheckNow(it)}
            onToggleExpand={() => onToggleExpand(it)}
            onLookup={onLookup}
            onChanged={onChanged}
          />
        );
      })}
    </div>
  );
}
