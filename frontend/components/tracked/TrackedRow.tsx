"use client";

import Link from "next/link";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  TriangleAlert,
  UserRound,
} from "lucide-react";
import { confirmDialog, toast } from "@/components/Toast";
import { Checkbox } from "@/components/ui/checkbox";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { fmtDateTime } from "@/lib/format";
import {
  TRACKED_LABELS,
  api,
  imgProxy,
  type MissingCodesResult,
  type MissingSummaryItem,
  type PresenceCodeFiles,
  type PresenceCodeLookup,
  type PresenceFileItem,
  type TrackedKind,
  type TrackedListing,
} from "@/lib/api";
import MissingDetailPanel from "./MissingDetailPanel";

export function keyOf(it: TrackedListing) {
  return `${it.kind}:${it.id}`;
}

const KIND_COLORS: Record<TrackedKind, string> = {
  star: "bg-pink-500/20 text-pink-300",
  studio: "bg-blue-500/20 text-blue-300",
  label: "bg-violet-500/20 text-violet-300",
  series: "bg-emerald-500/20 text-emerald-300",
  director: "bg-amber-500/20 text-amber-300",
  genre: "bg-cyan-500/20 text-cyan-300",
};

// 單一追蹤項目卡片。跨列共享的 state(missing / expanded / details /
// checkingKey…)都留在 page 層,這裡只收「已針對本列取值」的 props;
// 只跟本列有關、且只依賴 onChanged(=page 的 load)的動作
// (取消追蹤 / 切換自動送 / 清除新作品數)搬進來一起維護。
export default function TrackedRow({
  item,
  missing,
  missingLoading,
  expanded,
  isChecking,
  checkingPhase,
  checkDisabled,
  batchActive,
  detail,
  detailLoading,
  lookups,
  lookupBusy,
  codeFiles,
  codeFilesBusy,
  onCheckNow,
  onToggleExpand,
  onLookup,
  onLoadFiles,
  onPlay,
  onChanged,
}: {
  item: TrackedListing;
  missing: MissingSummaryItem | undefined;
  missingLoading: boolean;
  expanded: boolean;
  isChecking: boolean;
  checkingPhase: string;
  checkDisabled: boolean;
  batchActive: boolean;
  detail: MissingCodesResult | null;
  detailLoading: boolean;
  lookups: Map<string, PresenceCodeLookup>;
  lookupBusy: Set<string>;
  codeFiles: Map<string, PresenceCodeFiles>;
  codeFilesBusy: Set<string>;
  onCheckNow: () => void;
  onToggleExpand: () => void;
  onLookup: (code: string) => void;
  onLoadFiles: (code: string) => void;
  onPlay: (file: PresenceFileItem) => void;
  onChanged: () => void;
}) {
  async function remove() {
    const ok = await confirmDialog(
      `不再追蹤 ${TRACKED_LABELS[item.kind]} ${item.name || item.id}？`
    );
    if (!ok) return;
    try {
      await api.del(`/api/tracked/${item.kind}/${encodeURIComponent(item.id)}`);
      toast.success("已取消追蹤");
      onChanged();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function toggleAuto() {
    if (!item.auto_send && item.kind === "genre") {
      // Genre catalogs run to thousands of works — backfill would keep
      // feeding the queue batch after batch. Make sure it's deliberate.
      const ok = await confirmDialog(
        `「${item.name || item.id}」是類別追蹤,作品數可能上千部;開啟自動送出後` +
          "會分批把所有缺漏送進 PikPak。確定要開啟?"
      );
      if (!ok) return;
    }
    await api.post("/api/tracked", { ...item, auto_send: !item.auto_send });
    onChanged();
  }

  async function resetNew() {
    await api.post(
      `/api/tracked/${item.kind}/${encodeURIComponent(item.id)}/reset-new-count`
    );
    onChanged();
  }

  const expandChevron = expanded ? (
    <ChevronDown className="h-3 w-3" aria-hidden />
  ) : (
    <ChevronRight className="h-3 w-3" aria-hidden />
  );

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap gap-3 p-3">
        {item.avatar ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={imgProxy(item.avatar)}
            alt={item.name}
            referrerPolicy="no-referrer"
            className="h-20 w-16 flex-none rounded object-cover"
          />
        ) : (
          <div className="grid h-20 w-16 flex-none place-items-center rounded bg-muted text-muted-foreground/50">
            {item.kind === "star" ? (
              <UserRound className="h-6 w-6" aria-hidden />
            ) : (
              <Folder className="h-6 w-6" aria-hidden />
            )}
          </div>
        )}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={"rounded px-2 py-0.5 text-xs " + KIND_COLORS[item.kind]}
            >
              {TRACKED_LABELS[item.kind]}
            </span>
            <Link
              href={`/${item.kind}/${encodeURIComponent(item.id)}`}
              className="font-semibold text-primary hover:underline"
            >
              {item.name || item.id}
            </Link>
            {item.new_count > 0 && (
              <button
                onClick={resetNew}
                className="rounded bg-amber-500/20 px-2 py-0.5 text-xs text-amber-300 hover:bg-amber-500/30"
                title="點擊清除"
              >
                {item.new_count} 新
              </button>
            )}
            {(() => {
              const m = missing;
              if (missingLoading && !m) {
                return <StatusBadge tone="muted">缺漏…</StatusBadge>;
              }
              if (!m) return null;
              if (m.error) {
                return (
                  <span
                    className="cursor-help rounded bg-red-500/20 px-2 py-0.5 text-xs text-red-300"
                    title={m.error}
                  >
                    缺漏 ?
                  </span>
                );
              }
              const missingBadge =
                m.total === 0 ? (
                  <span
                    key="no-listing"
                    className="cursor-help rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                    title="JavBus 沒回傳列表(可能 slug 失效 / 網路 / 地區封鎖),所以無法判斷缺漏 / 多餘"
                  >
                    未取得列表
                  </span>
                ) : m.missing_count > 0 ? (
                  <button
                    key="missing"
                    onClick={onToggleExpand}
                    className="inline-flex items-center gap-1 rounded bg-amber-400/20 px-2 py-0.5 text-xs text-amber-300 hover:bg-amber-400/30"
                    title={`全集 ${m.total} 部,掃 ${m.pages_scanned} 頁 · 點擊看明細`}
                  >
                    {m.missing_count} 個未下載 {expandChevron}
                  </button>
                ) : (
                  <span
                    key="all-here"
                    className="cursor-help rounded bg-emerald-500/20 px-2 py-0.5 text-xs text-emerald-300"
                    title={`全集 ${m.total} 部都已下載`}
                  >
                    全收齊
                  </span>
                );
              const extrasBadge =
                m.extras_count > 0 ? (
                  <button
                    key="extras"
                    onClick={onToggleExpand}
                    className="inline-flex items-center gap-1 rounded bg-purple-500/20 px-2 py-0.5 text-xs text-purple-300 hover:bg-purple-500/30"
                    title="此資料夾裡有 JavBus 列表沒有的番號 · 點擊看明細"
                  >
                    {m.extras_count} 多餘 {expandChevron}
                  </button>
                ) : null;
              return (
                <>
                  {missingBadge}
                  {extrasBadge}
                </>
              );
            })()}
            {item.uncensored && <StatusBadge tone="neutral">無碼</StatusBadge>}
          </div>
          <div className="text-xs text-muted-foreground">
            slug: <span className="font-mono">{item.id}</span>
            {item.last_seen_code && (
              <>
                {" · 最後看到: "}
                <span className="font-mono">{item.last_seen_code}</span>
              </>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            最後檢查 {fmtDateTime(item.last_checked_at, "從未檢查")}
            {" · 缺漏掃描 "}
            {fmtDateTime(
              missing?.catalog_fetched_at ?? item.last_full_scan_at,
              "尚未掃描"
            )}
          </div>
          {item.last_error && (
            <div className="line-clamp-2 text-xs text-amber-300/80">
              <TriangleAlert
                className="mr-1 inline h-3 w-3 align-[-1px]"
                aria-hidden
              />
              {item.last_error}
            </div>
          )}
        </div>
        <div className="flex flex-col items-end gap-1 text-xs">
          <label
            className="flex items-center gap-1.5 text-muted-foreground"
            title="開啟後,排程檢查時會把 JavBus 列表上、PikPak 還沒有的番號都自動送上去(不只新作品),已送過的會用 btih 去重"
          >
            <Checkbox
              checked={item.auto_send}
              onCheckedChange={() => toggleAuto()}
            />
            自動送 PikPak
          </label>
          <div className="flex gap-2">
            <button
              onClick={onCheckNow}
              disabled={checkDisabled}
              className="text-blue-300 hover:underline disabled:opacity-50"
              title={
                batchActive
                  ? "批次掃描中"
                  : isChecking
                  ? checkingPhase || "檢查中"
                  : undefined
              }
            >
              {isChecking ? checkingPhase || "檢查中" : "立即檢查"}
            </button>
            <button onClick={remove} className="text-red-300 hover:underline">
              取消追蹤
            </button>
          </div>
        </div>
      </div>

      {expanded && (
        <MissingDetailPanel
          tracked={item}
          detail={detail}
          loading={detailLoading}
          lookups={lookups}
          lookupBusy={lookupBusy}
          codeFiles={codeFiles}
          codeFilesBusy={codeFilesBusy}
          onLookup={onLookup}
          onLoadFiles={onLoadFiles}
          onPlay={onPlay}
        />
      )}
    </div>
  );
}
