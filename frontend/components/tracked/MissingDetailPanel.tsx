"use client";

import Link from "next/link";
import { Check, TriangleAlert } from "lucide-react";
import type {
  MissingCodesResult,
  MovieListItem,
  PresenceCodeLookup,
  TrackedListing,
} from "@/lib/api";

// 追蹤列展開後的「缺漏 / 多餘番號」明細面板。純展示元件:資料
// (detail / lookups)與查詢動作(onLookup)都由 page 層持有並下傳,
// 收合再展開時才能沿用 page 層的快取。
export default function MissingDetailPanel({
  tracked,
  detail,
  loading,
  lookups,
  lookupBusy,
  onLookup,
}: {
  tracked: TrackedListing;
  detail: MissingCodesResult | null;
  loading: boolean;
  lookups: Map<string, PresenceCodeLookup>;
  lookupBusy: Set<string>;
  onLookup: (code: string) => void;
}) {
  function expectedPath(code: string) {
    // expected_root comes from the backend so it matches the actual
    // archiver sanitization (preserves kana, spaces, etc.).
    return `${detail?.expected_root || `AVBT/${tracked.kind}/${tracked.name || tracked.id}`}/${code}`;
  }
  if (loading && !detail) {
    return (
      <div className="border-t border-border px-4 py-3 text-xs text-muted-foreground">
        讀取缺漏明細中…
      </div>
    );
  }
  if (!detail) return null;
  if (!detail.missing.length && !detail.extras.length) {
    return (
      <div className="flex items-center gap-1 border-t border-border px-4 py-3 text-xs text-emerald-300/80">
        <Check className="h-3.5 w-3.5" aria-hidden />
        已無缺漏、也沒有多餘番號
      </div>
    );
  }
  return (
    <div className="border-t border-border px-4 py-3 text-xs">
      <div className="mb-2 space-y-1">
        <div className="text-muted-foreground">
          共 <span className="font-mono">{detail.total}</span> 部・已下載{" "}
          <span className="font-mono text-emerald-300/80">
            {detail.present_codes.length}
          </span>
          ・缺漏{" "}
          <span className="font-mono text-amber-300">
            {detail.missing.length}
          </span>
          {detail.extras.length > 0 && (
            <>
              ・多餘{" "}
              <span className="font-mono text-purple-300">
                {detail.extras.length}
              </span>
            </>
          )}
          <span className="ml-2 text-muted-foreground/70">
            (掃 {detail.pages_scanned} 頁)
          </span>
        </div>
        <div className="text-muted-foreground/70">
          判斷路徑:
          <span className="ml-1 font-mono text-foreground/70">
            {detail.expected_root}/&lt;番號&gt;
          </span>
        </div>
      </div>
      <ul className="divide-y divide-border/60">
        {detail.missing.map((m: MovieListItem) => {
          const lookup = lookups.get(m.code);
          const busy = lookupBusy.has(m.code);
          return (
            <li key={m.code} className="py-2">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <Link
                  href={`/movie/${encodeURIComponent(m.code)}`}
                  className="font-mono text-primary hover:underline"
                >
                  {m.code}
                </Link>
                <span className="truncate text-foreground/80" title={m.title}>
                  {m.title}
                </span>
                <span className="ml-auto flex items-center gap-2">
                  <span
                    className="font-mono text-muted-foreground/70"
                    title="archiver 預期會放這裡"
                  >
                    {expectedPath(m.code)}
                  </span>
                  <button
                    onClick={() => onLookup(m.code)}
                    disabled={busy || lookup !== undefined}
                    className="text-blue-300 hover:underline disabled:opacity-40"
                  >
                    {busy
                      ? "查詢中…"
                      : lookup !== undefined
                      ? "已查詢"
                      : "查實際位置"}
                  </button>
                </span>
              </div>
              {lookup && (
                <div className="mt-1 pl-2 text-[11px]">
                  {lookup.paths.length === 0 ? (
                    <span className="inline-flex items-center gap-1 text-amber-300/80">
                      <TriangleAlert className="h-3 w-3 shrink-0" aria-hidden />
                      索引裡找不到此番號(或在未被掃描的路徑下)
                    </span>
                  ) : (
                    <div className="space-y-0.5">
                      <span className="inline-flex items-center gap-1 text-emerald-300/80">
                        <Check className="h-3 w-3 shrink-0" aria-hidden />
                        實際在以下路徑找到({lookup.paths.length}):
                      </span>
                      {lookup.paths.map((p) => (
                        <div
                          key={p}
                          className="font-mono text-emerald-200/80"
                        >
                          ・{p}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {detail.extras.length > 0 && (
        <div className="mt-3 border-t border-border/60 pt-2">
          <div className="mb-1 text-muted-foreground">
            多餘番號 ({detail.extras.length})
            <span className="ml-1 text-muted-foreground/70">
              · 此資料夾裡有,但不在 JavBus 列表內
            </span>
          </div>
          <ul className="divide-y divide-border/60">
            {detail.extras.map((e) => (
              <li key={e.code} className="py-1.5">
                <div className="flex items-center gap-2">
                  <Link
                    href={`/movie/${encodeURIComponent(e.code)}`}
                    className="font-mono text-purple-300 hover:underline"
                  >
                    {e.code}
                  </Link>
                </div>
                {e.paths.map((p) => (
                  <div
                    key={p}
                    className="pl-2 font-mono text-[11px] text-muted-foreground/70"
                  >
                    ・{p}
                  </div>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
