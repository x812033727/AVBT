"use client";

import { useEffect, useRef, useState } from "react";
import { Check, TriangleAlert, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { streamNdjson } from "@/lib/api";

type Progress_ = {
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
  const [progress, setProgress] = useState<Progress_[]>([]);
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

  const percent = total ? Math.round((progress.length / total) * 100) : 0;
  const recent = progress.slice(-12).reverse();
  const errorCount = progress.filter((p) => p.error).length;
  const currentItem =
    busy && progress.length > 0
      ? progress[progress.length - 1]?.name ||
        `${progress[progress.length - 1]?.kind}/${progress[progress.length - 1]?.id}`
      : "";

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        // Dialog 的 ✕ / Esc / 點遮罩都會走這裡;busy 時 close() 內部會
        // 擋下,行為與舊版手寫遮罩一致(串流中不可關閉,僅能「取消」)。
        if (!o) close();
      }}
    >
      <DialogContent className="max-w-2xl" aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle>{cfg.title}</DialogTitle>
        </DialogHeader>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              {progress.length} / {total || "?"}
              {total > 0 && ` (${percent}%)`}
              {skipped > 0 && ` ・ 跳過 ${skipped} 個齊全`}
            </span>
            {errorCount > 0 && (
              <span className="text-red-300">失敗 {errorCount}</span>
            )}
          </div>
          <Progress value={percent} />
          {busy && currentItem && (
            <div className="truncate rounded-md border border-blue-400/30 bg-blue-500/5 px-2 py-1 text-xs text-blue-200">
              處理中: <span className="font-mono">{currentItem}</span>
            </div>
          )}
          <ul className="max-h-72 overflow-y-auto rounded-md border border-border bg-background/50 p-2 text-xs">
            {recent.length === 0 && (
              <li className="text-muted-foreground/70">
                {busy ? "等待第一筆…" : "尚無資料"}
              </li>
            )}
            {recent.map((p) => (
              <li
                key={`${p.kind}:${p.id}:${p.current}`}
                className="flex items-center gap-2 py-0.5"
              >
                <span className="w-12 text-muted-foreground/70">
                  {p.current}/{p.total}
                </span>
                {p.error ? (
                  <X className="h-3 w-3 shrink-0 text-red-300" aria-hidden />
                ) : (p.missing_count ?? 0) > 0 ? (
                  <TriangleAlert
                    className="h-3 w-3 shrink-0 text-amber-300"
                    aria-hidden
                  />
                ) : (
                  <Check
                    className="h-3 w-3 shrink-0 text-emerald-300"
                    aria-hidden
                  />
                )}
                <span className="truncate text-foreground/80">
                  {p.name || `${p.kind}/${p.id}`}
                </span>
                {p.error ? (
                  <span className="truncate text-red-300/80">{p.error}</span>
                ) : (
                  <>
                    {p.missing_count !== undefined && (
                      <span className="text-muted-foreground">
                        缺漏 {p.missing_count}
                      </span>
                    )}
                    {p.pages_scanned !== undefined && p.pages_scanned > 0 && (
                      <span className="text-muted-foreground/70">
                        ({p.pages_scanned} 頁)
                      </span>
                    )}
                    {p.new_codes && p.new_codes.length > 0 && (
                      <span className="font-mono text-primary">
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

        {errorMsg && <ErrorBox message={errorMsg} />}

        {done && (
          <div className="space-y-1 rounded-md border border-border bg-muted/50 px-3 py-2 text-sm">
            <div>
              完成 <strong>{progress.length}</strong> 個 listing
              {skipped > 0 && ` (跳過 ${skipped} 個齊全的)`}
            </div>
            {errorCount > 0 && (
              <div className="flex items-center gap-1 text-red-300">
                <X className="h-3.5 w-3.5" aria-hidden />
                失敗 {errorCount}
              </div>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2">
          {busy ? (
            <Button variant="ghost" onClick={cancel}>
              取消
            </Button>
          ) : (
            <Button onClick={close}>關閉</Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
