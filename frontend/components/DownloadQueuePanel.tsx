"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { api, type QueueStatus } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import type { StatusTone } from "@/lib/status";
import { StatusBadge } from "@/components/shared/StatusBadge";

const STATUS_VIEW: Record<string, { tone: StatusTone; label: string }> = {
  sent: { tone: "success", label: "已送" },
  skipped_no_magnet: { tone: "muted", label: "無磁力" },
  skipped_already_sent: { tone: "muted", label: "已送過" },
  failed: { tone: "danger", label: "失敗" },
  cancelled: { tone: "muted", label: "取消" },
};

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
  const skippedTotal =
    status.totals.skipped_already_sent + status.totals.skipped_no_magnet;

  return (
    <div className="rounded-md border border-border bg-muted/30">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-3 py-2 text-left text-xs transition hover:bg-muted/50"
      >
        <span className="font-semibold text-foreground/80">下載佇列</span>
        {active > 0 ? (
          <>
            <span className="rounded bg-primary/20 px-1.5 py-0.5 font-mono text-primary">
              {status.processing.length} / {status.concurrency} 進行 · {status.pending} 待送
            </span>
            <span className="text-muted-foreground">
              {status.processing.slice(0, 3).map((p) => p.code).join("  ")}
              {status.processing.length > 3 && " …"}
            </span>
          </>
        ) : (
          <span className="text-muted-foreground">閒置</span>
        )}
        <span className="ml-auto text-muted-foreground tabular-nums">
          累計 <span className="text-emerald-300">{status.totals.sent} 送</span>{" "}
          <span className="text-muted-foreground/70">{skippedTotal} 略過</span>{" "}
          <span className="text-red-300">{status.totals.failed} 失敗</span>
        </span>
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
        )}
      </button>

      {open && (
        <div className="space-y-3 border-t border-border px-3 py-2 text-xs">
          {status.processing.length > 0 && (
            <div>
              <div className="mb-1 text-muted-foreground">處理中</div>
              <ul className="space-y-0.5">
                {status.processing.map((p) => (
                  <li key={p.code + p.source} className="flex gap-2">
                    <span className="font-mono text-primary">{p.code}</span>
                    <span className="text-muted-foreground">{p.source}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {status.recent.length > 0 && (
            <div>
              <div className="mb-1 text-muted-foreground">最近 {status.recent.length} 筆</div>
              <ul className="max-h-48 space-y-0.5 overflow-y-auto">
                {status.recent.slice(0, 20).map((r, i) => {
                  const view = STATUS_VIEW[r.status] ?? {
                    tone: "neutral" as StatusTone,
                    label: r.status,
                  };
                  return (
                    <li key={i} className="flex items-baseline gap-2">
                      <span className="font-mono text-[10px] text-muted-foreground/70">
                        {fmtTime(r.at)}
                      </span>
                      <span className="font-mono text-primary">{r.code}</span>
                      <StatusBadge tone={view.tone}>{view.label}</StatusBadge>
                      {r.magnet_name && (
                        <span className="truncate text-muted-foreground">{r.magnet_name}</span>
                      )}
                      {r.message && !r.magnet_name && (
                        <span className="truncate text-muted-foreground">{r.message}</span>
                      )}
                      <span className="ml-auto text-muted-foreground/70">{r.source}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {!status.processing.length && !status.recent.length && (
            <div className="text-muted-foreground">尚無下載紀錄</div>
          )}
        </div>
      )}
    </div>
  );
}
