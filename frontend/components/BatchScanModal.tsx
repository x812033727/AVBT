"use client";

import { useEffect, useRef, useState } from "react";
import { streamNdjson } from "@/lib/api";

type Progress = {
  current: number;
  total: number;
  kind: string;
  id: string;
  name?: string;
  // check_all_stream payload
  new_codes?: string[];
  // missing_summary_stream payload
  missing_count?: number;
  pages_scanned?: number;
  error?: string | null;
};

type Mode = "check-all" | "missing-summary";

const MODE_CONFIG: Record<Mode, { title: string; endpoint: string }> = {
  "check-all": {
    title: "全部立即檢查",
    endpoint: "/api/tracked/status/run-now/stream",
  },
  "missing-summary": {
    title: "重算缺漏",
    endpoint: "/api/tracked/missing-summary/stream?refresh=true",
  },
};

export default function BatchScanModal({
  open,
  mode,
  onClose,
  onDone,
  onProgress,
}: {
  open: boolean;
  mode: Mode;
  onClose: () => void;
  onDone?: () => void;
  // Fires for every per-listing ``progress`` event from the stream.
  // Lets the parent patch its row state (items / missing Map) live as
  // each listing completes, instead of waiting for the whole batch.
  onProgress?: (event: any) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [total, setTotal] = useState(0);
  const [skipped, setSkipped] = useState(0);
  const [progress, setProgress] = useState<Progress[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const cfg = MODE_CONFIG[mode];

  // Auto-start the stream when the modal opens, and reset state on close.
  useEffect(() => {
    if (!open) {
      // Reset on close so the next open starts fresh.
      setProgress([]);
      setTotal(0);
      setSkipped(0);
      setErrorMsg(null);
      setDone(false);
      return;
    }
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setBusy(true);
    setDone(false);
    setProgress([]);
    setTotal(0);
    setSkipped(0);
    setErrorMsg(null);
    (async () => {
      try {
        await streamNdjson(
          cfg.endpoint,
          {},
          (event) => {
            if (event.type === "start") {
              setTotal(event.total ?? 0);
              setSkipped(event.skipped ?? 0);
            } else if (event.type === "progress") {
              setProgress((prev) => [...prev, event]);
              onProgress?.(event);
            } else if (event.type === "done") {
              setDone(true);
            } else if (event.type === "error") {
              setErrorMsg(event.message ?? "未知錯誤");
            }
          },
          ctrl.signal,
        );
        onDone?.();
      } catch (e: any) {
        if (e.name !== "AbortError") {
          setErrorMsg(e.message ?? String(e));
        }
      } finally {
        setBusy(false);
        abortRef.current = null;
      }
    })();
    return () => {
      ctrl.abort();
    };
  // cfg.endpoint is mode-derived; modal stays mounted across a single
  // session and either runs once or is aborted when closed.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode]);

  function cancel() {
    abortRef.current?.abort();
  }

  function close() {
    if (busy) return;
    onClose();
  }

  if (!open) return null;

  const percent = total ? Math.round((progress.length / total) * 100) : 0;
  const recent = progress.slice(-12).reverse();
  const errorCount = progress.filter((p) => p.error).length;
  const currentItem =
    busy && progress.length > 0
      ? progress[progress.length - 1]?.name ||
        `${progress[progress.length - 1]?.kind}/${progress[progress.length - 1]?.id}`
      : "";

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div className="w-full max-w-2xl space-y-4 rounded-xl border border-white/10 bg-panel p-5">
        <div className="flex items-center">
          <h2 className="text-lg font-semibold">{cfg.title}</h2>
          <button
            className="ml-auto text-white/40 hover:text-white disabled:opacity-30"
            onClick={close}
            disabled={busy}
          >
            ✕
          </button>
        </div>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-white/60">
            <span>
              {progress.length} / {total || "?"}
              {total > 0 && ` (${percent}%)`}
              {skipped > 0 && ` ・ 跳過 ${skipped} 個齊全`}
            </span>
            {errorCount > 0 && (
              <span className="text-red-300">失敗 {errorCount}</span>
            )}
          </div>
          <div className="h-2 overflow-hidden rounded bg-white/10">
            <div
              className="h-full bg-accent transition-[width]"
              style={{ width: `${percent}%` }}
            />
          </div>
          {busy && currentItem && (
            <div className="truncate rounded-md border border-blue-400/30 bg-blue-500/5 px-2 py-1 text-xs text-blue-200">
              處理中: <span className="font-mono">{currentItem}</span>
            </div>
          )}
          <ul className="max-h-72 overflow-y-auto rounded-md border border-white/10 bg-ink/50 p-2 text-xs">
            {recent.length === 0 && (
              <li className="text-white/40">
                {busy ? "等待第一筆…" : "尚無資料"}
              </li>
            )}
            {recent.map((p) => (
              <li
                key={`${p.kind}:${p.id}:${p.current}`}
                className="flex items-baseline gap-2 py-0.5"
              >
                <span className="w-12 text-white/40">
                  {p.current}/{p.total}
                </span>
                <span
                  className={
                    p.error
                      ? "text-red-300"
                      : (p.missing_count ?? 0) > 0
                        ? "text-amber-300"
                        : "text-emerald-300"
                  }
                >
                  {p.error
                    ? "✗"
                    : (p.missing_count ?? 0) > 0
                      ? "⚠"
                      : "✓"}
                </span>
                <span className="truncate text-white/70">
                  {p.name || `${p.kind}/${p.id}`}
                </span>
                {p.error ? (
                  <span className="truncate text-red-300/80">{p.error}</span>
                ) : (
                  <>
                    {p.missing_count !== undefined && (
                      <span className="text-white/50">
                        缺漏 {p.missing_count}
                      </span>
                    )}
                    {p.pages_scanned !== undefined && p.pages_scanned > 0 && (
                      <span className="text-white/40">
                        ({p.pages_scanned} 頁)
                      </span>
                    )}
                    {p.new_codes && p.new_codes.length > 0 && (
                      <span className="font-mono text-accent">
                        +{p.new_codes.length}: {p.new_codes.slice(0, 3).join(", ")}
                        {p.new_codes.length > 3 ? "…" : ""}
                      </span>
                    )}
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>

        {errorMsg && (
          <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {errorMsg}
          </div>
        )}

        {done && (
          <div className="space-y-1 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
            <div>
              完成 <strong>{progress.length}</strong> 個 listing
              {skipped > 0 && ` (跳過 ${skipped} 個齊全的)`}
            </div>
            {errorCount > 0 && (
              <div className="text-red-300">✗ 失敗 {errorCount}</div>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2">
          {busy ? (
            <button className="btn-ghost" onClick={cancel}>
              取消
            </button>
          ) : (
            <button className="btn-primary" onClick={close}>
              關閉
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
