"use client";

import { AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";

export type PresenceMeta = {
  total: number;
  missing: number;
  extras: number;
  expected_root: string;
};

// 追蹤 listing 的存在感摘要卡:抽出自 ListingPage,供 star 頁重用(取得
// presence overlay 而不必重寫)。純展示元件:資料/動作皆由呼叫端傳入。
export function PresenceSummary({
  meta,
  error,
  busy,
  onRefresh,
  hideDownloaded,
  onHideDownloadedChange,
}: {
  meta: PresenceMeta | null;
  error: string | null;
  busy: boolean;
  onRefresh: () => void;
  /** 目前分頁是否隱藏已下載;僅在 meta 存在(即已有 presence 資料)時顯示切換開關 */
  hideDownloaded?: boolean;
  onHideDownloadedChange?: (v: boolean) => void;
}) {
  if (meta) {
    const archived = meta.total - meta.missing;
    const pct = meta.total > 0 ? Math.round((100 * archived) / meta.total) : 0;
    return (
      <div className="space-y-1.5 rounded-lg border border-border bg-card/50 px-3 py-2 text-xs text-muted-foreground">
        <div className="flex flex-wrap items-center gap-3">
          <span>
            追蹤全集共{" "}
            <span className="font-semibold text-foreground">{meta.total}</span> 部 ・{" "}
            <span className="text-emerald-300">已下載 {archived}</span> ／{" "}
            <span className="text-amber-300">缺漏 {meta.missing}</span>
            {meta.extras > 0 && (
              <>
                {" "}
                ・{" "}
                <span
                  className="text-purple-300"
                  title="此資料夾有,但不在 JavBus 列表內的番號"
                >
                  多餘 {meta.extras}
                </span>
              </>
            )}
          </span>
          <Button
            variant="outline"
            size="sm"
            className="ml-auto h-6 px-2 text-xs"
            onClick={onRefresh}
            disabled={busy}
          >
            {busy ? "重建中…" : "重新整理 PikPak 索引"}
          </Button>
        </div>
        {meta.total > 0 && (
          <div className="flex items-center gap-2">
            <span className="shrink-0 font-mono tabular-nums text-foreground/80">
              已收 {pct}%
            </span>
            <div className="h-1 w-full max-w-[10rem] overflow-hidden rounded-full bg-muted/60">
              <div
                className="h-full rounded-full bg-primary/60"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        )}
        {meta.expected_root && (
          <div className="text-muted-foreground/70">
            判斷路徑:
            <span className="ml-1 font-mono text-foreground/70">
              {meta.expected_root}/&lt;番號&gt;
            </span>
          </div>
        )}
        {onHideDownloadedChange && (
          <div className="flex items-center gap-1.5 pt-0.5">
            <Checkbox
              id="listing-hide-downloaded"
              checked={!!hideDownloaded}
              onCheckedChange={(v) => onHideDownloadedChange(v === true)}
            />
            <Label
              htmlFor="listing-hide-downloaded"
              title="僅套用於目前分頁(伺服器端分頁,無法預先過濾其他頁)"
              className="cursor-help text-xs font-normal text-muted-foreground"
            >
              隱藏已下載
            </Label>
          </div>
        )}
      </div>
    );
  }

  if (error) {
    return (
      <div
        role="alert"
        className="flex flex-wrap items-center gap-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300"
      >
        <span className="inline-flex items-center gap-2">
          <AlertCircle className="h-4 w-4 shrink-0" aria-hidden />
          <span>
            缺漏讀取失敗:{error}
            <span className="ml-1 text-red-300/70">
              (JavBus 可能限流 / 暫時無法連線,稍後再試)
            </span>
          </span>
        </span>
        <button
          type="button"
          onClick={onRefresh}
          disabled={busy}
          className="ml-auto rounded-md border border-red-400/30 px-2 py-0.5 transition hover:bg-red-500/15 disabled:opacity-40"
        >
          {busy ? "重試中…" : "重試"}
        </button>
      </div>
    );
  }

  return null;
}
