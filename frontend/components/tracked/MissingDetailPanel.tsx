"use client";

import { useState } from "react";
import Link from "next/link";
import { Check, Play, TriangleAlert } from "lucide-react";
import type {
  MissingCodesResult,
  MovieListItem,
  PresenceCodeFiles,
  PresenceCodeLookup,
  PresenceFileItem,
  TrackedListing,
} from "@/lib/api";

// 一次先渲染這麼多筆已下載番號,點「顯示全部」才放開 — 大列表(數百部)
// 全量渲染會卡頓,而且每列的播放查詢本來就是點了才發。
const PRESENT_PREVIEW = 50;

function fmtSize(size?: number | null) {
  if (typeof size !== "number" || size <= 0) return "";
  return `${(size / 1024 ** 3).toFixed(1)} GB`;
}

// 追蹤列展開後的「缺漏 / 已下載 / 多餘番號」明細面板。純展示元件:資料
// (detail / lookups / codeFiles)與查詢動作(onLookup / onLoadFiles)都由
// page 層持有並下傳,收合再展開時才能沿用 page 層的快取。
export default function MissingDetailPanel({
  tracked,
  detail,
  loading,
  lookups,
  lookupBusy,
  codeFiles,
  codeFilesBusy,
  onLookup,
  onLoadFiles,
  onPlay,
}: {
  tracked: TrackedListing;
  detail: MissingCodesResult | null;
  loading: boolean;
  lookups: Map<string, PresenceCodeLookup>;
  lookupBusy: Set<string>;
  codeFiles: Map<string, PresenceCodeFiles>;
  codeFilesBusy: Set<string>;
  onLookup: (code: string) => void;
  onLoadFiles: (code: string) => void;
  onPlay: (file: PresenceFileItem) => void;
}) {
  const [showPresent, setShowPresent] = useState(false);
  const [presentLimit, setPresentLimit] = useState(PRESENT_PREVIEW);

  function expectedPath(code: string) {
    // expected_root comes from the backend so it matches the actual
    // archiver sanitization (preserves kana, spaces, etc.).
    return `${detail?.expected_root || `AVBT/${tracked.kind}/${tracked.name || tracked.id}`}/${code}`;
  }

  function playButton(code: string) {
    const busy = codeFilesBusy.has(code);
    return (
      <button
        onClick={() => onLoadFiles(code)}
        disabled={busy}
        className="inline-flex items-center gap-1 text-emerald-300 hover:underline disabled:opacity-40"
      >
        <Play className="h-3 w-3 shrink-0" aria-hidden />
        {busy ? "查詢中…" : "播放"}
      </button>
    );
  }

  // 查過檔案後的結果區:多支列出逐檔播放;單支也照列(重播入口);
  // 空結果標示找不到。點列表裡的「播放」直接開 VideoPlayerModal。
  function filesBlock(code: string) {
    const res = codeFiles.get(code);
    if (!res) return null;
    return (
      <div className="mt-1 pl-2 text-[11px]">
        {res.files.length === 0 ? (
          <span className="inline-flex items-center gap-1 text-amber-300/80">
            <TriangleAlert className="h-3 w-3 shrink-0" aria-hidden />
            PikPak 上找不到影片檔
          </span>
        ) : (
          <div className="space-y-0.5">
            {res.files.map((f) => (
              <div key={f.id} className="flex items-center gap-2">
                <button
                  onClick={() => onPlay(f)}
                  className="inline-flex shrink-0 items-center gap-1 text-emerald-300 hover:underline"
                >
                  <Play className="h-3 w-3" aria-hidden />
                  播放
                </button>
                <span
                  className="truncate font-mono text-foreground/70"
                  title={f.path || f.name}
                >
                  {f.name}
                </span>
                {fmtSize(f.size) && (
                  <span className="shrink-0 text-muted-foreground/60">
                    {fmtSize(f.size)}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  if (loading && !detail) {
    return (
      <div className="border-t border-border px-4 py-3 text-xs text-muted-foreground">
        讀取缺漏明細中…
      </div>
    );
  }
  if (!detail) return null;

  const presentShown = detail.present_codes.slice(0, presentLimit);

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
      {!detail.missing.length && !detail.extras.length && (
        <div className="mb-2 flex items-center gap-1 text-emerald-300/80">
          <Check className="h-3.5 w-3.5" aria-hidden />
          已無缺漏、也沒有多餘番號
        </div>
      )}
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
      {detail.present_codes.length > 0 && (
        <div className="mt-3 border-t border-border/60 pt-2">
          <button
            onClick={() => setShowPresent((v) => !v)}
            className="mb-1 text-emerald-300/90 hover:underline"
          >
            已下載 ({detail.present_codes.length}){" "}
            {showPresent ? "▲ 收合" : "▼ 展開播放"}
          </button>
          {showPresent && (
            <>
              <ul className="divide-y divide-border/60">
                {presentShown.map((code) => (
                  <li key={code} className="py-1.5">
                    <div className="flex items-center gap-3">
                      <Link
                        href={`/movie/${encodeURIComponent(code)}`}
                        className="font-mono text-primary hover:underline"
                      >
                        {code}
                      </Link>
                      <span className="ml-auto">{playButton(code)}</span>
                    </div>
                    {filesBlock(code)}
                  </li>
                ))}
              </ul>
              {detail.present_codes.length > presentLimit && (
                <button
                  onClick={() => setPresentLimit(detail.present_codes.length)}
                  className="mt-1 text-muted-foreground hover:underline"
                >
                  顯示全部(還有 {detail.present_codes.length - presentLimit} 部)
                </button>
              )}
            </>
          )}
        </div>
      )}
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
                  <span className="ml-auto">{playButton(e.code)}</span>
                </div>
                {e.paths.map((p) => (
                  <div
                    key={p}
                    className="pl-2 font-mono text-[11px] text-muted-foreground/70"
                  >
                    ・{p}
                  </div>
                ))}
                {filesBlock(e.code)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
