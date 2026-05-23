"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type QueueStatus } from "@/lib/api";

const STATUS_STYLES: Record<string, string> = {
  sent: "text-emerald-300",
  skipped_no_magnet: "text-white/40",
  skipped_already_sent: "text-white/40",
  failed: "text-red-300",
  cancelled: "text-white/30",
};

const STATUS_LABEL: Record<string, string> = {
  sent: "✓ 已送",
  skipped_no_magnet: "⏭ 無磁力",
  skipped_already_sent: "⏭ 已送過",
  failed: "✗ 失敗",
  cancelled: "○ 取消",
};

function formatTime(iso: string): string {
  try {
    return new Date(iso + (iso.endsWith("Z") ? "" : "Z")).toLocaleTimeString();
  } catch {
    return iso;
  }
}

function statusUnchanged(prev: QueueStatus, next: QueueStatus): boolean {
  // The panel only renders pending count, processing list, totals and
  // recent length. If none of those changed we can skip the re-render
  // — important because the poll fires every few seconds and React
  // would otherwise rebuild the whole subtree on each tick.
  if (prev.pending !== next.pending) return false;
  if (prev.concurrency !== next.concurrency) return false;
  if (prev.processing.length !== next.processing.length) return false;
  for (let i = 0; i < prev.processing.length; i++) {
    if (prev.processing[i].code !== next.processing[i].code) return false;
    if (prev.processing[i].source !== next.processing[i].source) return false;
  }
  const t1 = prev.totals;
  const t2 = next.totals;
  if (
    t1.sent !== t2.sent
    || t1.failed !== t2.failed
    || t1.skipped_no_magnet !== t2.skipped_no_magnet
    || t1.skipped_already_sent !== t2.skipped_already_sent
    || t1.cancelled !== t2.cancelled
  ) return false;
  if (prev.recent.length !== next.recent.length) return false;
  // Cheap top-of-list signature: if the most-recent entry changed, the
  // recent list churned and we need to re-render.
  if (prev.recent[0]?.at !== next.recent[0]?.at) return false;
  return true;
}

export default function DownloadQueuePanel({ refreshMs = 6000 }: { refreshMs?: number }) {
  const [status, setStatus] = useState<QueueStatus | null>(null);
  const [open, setOpen] = useState(false);
  const timerRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const s = await api.get<QueueStatus>("/api/pikpak/queue");
      setStatus((prev) => (prev && statusUnchanged(prev, s) ? prev : s));
    } catch {
      /* ignore — keep last good snapshot */
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (timerRef.current) window.clearInterval(timerRef.current);
    // Poll faster when there's activity so the user sees progress; slow
    // down to refreshMs when idle so we don't hammer the API.
    const interval = status && (status.pending > 0 || status.processing.length > 0) ? 3000 : refreshMs;
    timerRef.current = window.setInterval(load, interval);
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
    };
  }, [status, refreshMs, load]);

  if (!status) return null;

  const active = status.pending + status.processing.length;

  return (
    <div className="rounded-md border border-white/10 bg-white/5">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 px-3 py-2 text-left text-xs hover:bg-white/5"
      >
        <span className="font-semibold text-white/80">下載佇列</span>
        {active > 0 ? (
          <>
            <span className="rounded bg-accent/20 px-1.5 py-0.5 font-mono text-accent">
              {status.processing.length} / {status.concurrency} 進行 · {status.pending} 待送
            </span>
            <span className="text-white/40">
              {status.processing.slice(0, 3).map((p) => p.code).join("  ")}
              {status.processing.length > 3 && " …"}
            </span>
          </>
        ) : (
          <span className="text-white/50">閒置</span>
        )}
        <span className="ml-auto text-white/40">
          累計 ✓{status.totals.sent} ⏭{status.totals.skipped_already_sent + status.totals.skipped_no_magnet} ✗{status.totals.failed}
        </span>
        <span className="text-white/40">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="space-y-3 border-t border-white/10 px-3 py-2 text-xs">
          {status.processing.length > 0 && (
            <div>
              <div className="mb-1 text-white/50">處理中</div>
              <ul className="space-y-0.5">
                {status.processing.map((p) => (
                  <li key={p.code + p.source} className="flex gap-2">
                    <span className="font-mono text-accent">{p.code}</span>
                    <span className="text-white/40">{p.source}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {status.recent.length > 0 && (
            <div>
              <div className="mb-1 text-white/50">最近 {status.recent.length} 筆</div>
              <ul className="max-h-48 space-y-0.5 overflow-y-auto">
                {status.recent.slice(0, 20).map((r, i) => (
                  <li key={i} className="flex items-baseline gap-2">
                    <span className="font-mono text-white/30 text-[10px]">
                      {formatTime(r.at)}
                    </span>
                    <span className="font-mono text-accent">{r.code}</span>
                    <span className={STATUS_STYLES[r.status] ?? "text-white/60"}>
                      {STATUS_LABEL[r.status] ?? r.status}
                    </span>
                    {r.magnet_name && (
                      <span className="truncate text-white/40">{r.magnet_name}</span>
                    )}
                    {r.message && !r.magnet_name && (
                      <span className="truncate text-white/40">{r.message}</span>
                    )}
                    <span className="ml-auto text-white/30">{r.source}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {!status.processing.length && !status.recent.length && (
            <div className="text-white/40">尚無下載紀錄</div>
          )}
        </div>
      )}
    </div>
  );
}
