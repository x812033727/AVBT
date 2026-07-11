"use client";

import { useMemo, useRef, useState } from "react";
import {
  Clapperboard,
  Pencil,
  SkipForward,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { confirmDialog, toast } from "@/components/Toast";
import { streamNdjson } from "@/lib/api";
import { fmtBytes } from "@/lib/format";

type Episode = {
  file_id: string;
  name: string;
  code: string;
  category: "canonical" | "multifile";
  marker_index: number;
  parent_id: string;
  parent_path: string;
  size: number | null;
};

type Phase =
  | "idle"
  | "scanning"
  | "review"
  | "processing"
  | "done";

export default function EpisodeFinderButton({
  folder_id,
  folder_name,
  disabled,
  onDone,
  apiBase = "/api/pikpak",
}: {
  folder_id: string;
  folder_name: string;
  disabled?: boolean;
  onDone?: () => void;
  // Provider endpoint root. Defaults to PikPak; pass "/api/pcloud" to
  // drive the pCloud episode finder (same NDJSON contract).
  apiBase?: string;
}) {
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [foldersDone, setFoldersDone] = useState(0);
  const [foldersQueued, setFoldersQueued] = useState(0);
  const [filesSeen, setFilesSeen] = useState(0);
  const [partial, setPartial] = useState(false);
  const [items, setItems] = useState<Episode[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [autoStrip, setAutoStrip] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [trashProgress, setTrashProgress] = useState<{ current: number; total: number } | null>(null);
  const [stripEvents, setStripEvents] = useState<
    { source: string; target: string; action: string }[]
  >([]);
  const [summary, setSummary] = useState<{ trashed: number; renamed: number; skipped: number } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Group items by code for the review list.
  const grouped = useMemo(() => {
    const m = new Map<string, Episode[]>();
    for (const ep of items) {
      const list = m.get(ep.code) || [];
      list.push(ep);
      m.set(ep.code, list);
    }
    // Sort by code; inside each group sort by parent_path then name.
    const out = Array.from(m.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([code, eps]) => ({
        code,
        eps: eps.sort((a, b) =>
          (a.parent_path + a.name).localeCompare(b.parent_path + b.name)
        ),
      }));
    return out;
  }, [items]);

  function reset() {
    setPhase("idle");
    setFoldersDone(0);
    setFoldersQueued(0);
    setFilesSeen(0);
    setPartial(false);
    setItems([]);
    setSelected(new Set());
    setError(null);
    setWarnings([]);
    setTrashProgress(null);
    setStripEvents([]);
    setSummary(null);
  }

  function close() {
    if (phase === "scanning" || phase === "processing") return;
    setOpen(false);
    reset();
  }

  async function startScan() {
    reset();
    setOpen(true);
    setPhase("scanning");
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamNdjson(
        `${apiBase}/files/episodes/scan/stream`,
        { folder_id },
        (ev) => {
          if (ev.type === "scan_progress") {
            setFoldersDone(ev.folders_done ?? 0);
            setFoldersQueued(ev.folders_queued ?? 0);
            setFilesSeen(ev.files_seen ?? 0);
          } else if (ev.type === "item") {
            setItems((prev) => [...prev, ev.episode as Episode]);
          } else if (ev.type === "warn") {
            setWarnings((prev) => [
              ...prev,
              `${ev.folder_path || ""} ${ev.message || ""}`.trim(),
            ]);
          } else if (ev.type === "done") {
            setPartial(Boolean(ev.summary?.partial));
            setFoldersDone(ev.summary?.folders_visited ?? foldersDone);
            setFilesSeen(ev.summary?.files_scanned ?? filesSeen);
            setPhase("review");
          } else if (ev.type === "error") {
            setError(ev.message || "未知錯誤");
          }
        },
        ctrl.signal
      );
    } catch (e: any) {
      if (e.name !== "AbortError") setError(e.message);
      setPhase("idle");
    } finally {
      abortRef.current = null;
    }
  }

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  function selectAll() {
    setSelected(new Set(items.map((i) => i.file_id)));
  }
  function selectNone() {
    setSelected(new Set());
  }
  function selectMultifileOnly() {
    setSelected(
      new Set(items.filter((i) => i.category === "multifile").map((i) => i.file_id))
    );
  }
  function selectAllButLargestPerGroup() {
    // For each code-group, keep the largest video UNCHECKED, check the rest.
    const next = new Set<string>();
    for (const { eps } of grouped) {
      if (eps.length <= 1) continue;
      const sorted = [...eps].sort((a, b) => (b.size || 0) - (a.size || 0));
      for (let i = 1; i < sorted.length; i++) next.add(sorted[i].file_id);
    }
    setSelected(next);
  }

  async function runProcess() {
    if (phase === "processing") return;
    const ids = Array.from(selected);
    if (!ids.length && !autoStrip) {
      toast.info("沒有選取任何項目");
      return;
    }
    if (ids.length) {
      const ok = await confirmDialog(
        `移到垃圾桶 ${ids.length} 個檔案？`,
        autoStrip ? "完成後將自動去除剩餘 _N 標記" : undefined
      );
      if (!ok) return;
    }
    setPhase("processing");
    setTrashProgress(ids.length ? { current: 0, total: ids.length } : null);
    setStripEvents([]);
    setSummary(null);
    const parentIds = Array.from(
      new Set(
        items
          .filter((i) => ids.includes(i.file_id))
          .map((i) => i.parent_id)
      )
    );
    // If no files were selected but auto_strip is on, still tell the
    // backend to look at every parent folder we saw — the user might
    // have manually deleted files outside this modal.
    const fallbackParents = parentIds.length
      ? parentIds
      : Array.from(new Set(items.map((i) => i.parent_id)));
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamNdjson(
        `${apiBase}/files/episodes/process/stream`,
        {
          file_ids_to_trash: ids,
          parent_ids_touched: fallbackParents,
          auto_strip: autoStrip,
        },
        (ev) => {
          if (ev.type === "trash_progress") {
            setTrashProgress({ current: ev.current, total: ev.total });
          } else if (ev.type === "trash_done") {
            // ok
          } else if (ev.type === "strip_progress") {
            setStripEvents((prev) => [
              ...prev,
              {
                source: ev.source || "",
                target: ev.target || "",
                action: ev.action || "",
              },
            ]);
          } else if (ev.type === "warn") {
            setWarnings((prev) => [...prev, ev.message || ""]);
          } else if (ev.type === "done") {
            setSummary(ev.result);
            setPhase("done");
          } else if (ev.type === "error") {
            setError(ev.message || "未知錯誤");
          }
        },
        ctrl.signal
      );
      toast.success(
        `已刪除 ${summary?.trashed ?? ids.length} 個檔案${
          autoStrip ? `，去分集 ${summary?.renamed ?? 0} 個` : ""
        }`
      );
      onDone?.();
    } catch (e: any) {
      if (e.name !== "AbortError") setError(e.message);
      setPhase("review");
    } finally {
      abortRef.current = null;
    }
  }

  function cancel() {
    abortRef.current?.abort();
  }

  const scanPercent = foldersQueued + foldersDone
    ? Math.round((foldersDone / (foldersDone + foldersQueued)) * 100)
    : 0;

  const trashPercent = trashProgress?.total
    ? Math.round((trashProgress.current / trashProgress.total) * 100)
    : 0;

  return (
    <>
      {/* 包一層 span 承載 title:Button disabled 時 pointer-events-none,
          tooltip 才不會在停用狀態消失(原生按鈕停用時仍看得到提示)。 */}
      <span
        className="inline-flex"
        title={
          disabled
            ? "請先進入子資料夾（不可在雲端根目錄執行）"
            : "遞迴搜尋此資料夾及子目錄,列出所有分集檔案"
        }
      >
        <Button
          variant="outline"
          size="sm"
          onClick={startScan}
          disabled={disabled || phase === "scanning"}
        >
          <Clapperboard aria-hidden />
          搜尋分集
        </Button>
      </span>

      <Dialog
        open={open}
        onOpenChange={(o) => {
          // ✕ / Esc / 點遮罩統一走 close();掃描或處理中 close() 會擋下,
          // 行為與舊版手寫遮罩一致(進行中只能按「取消」中斷串流)。
          if (!o) close();
        }}
      >
        <DialogContent
          className="flex max-h-[calc(100vh-6rem)] w-full max-w-3xl flex-col gap-0 p-0"
          aria-describedby={undefined}
        >
          <DialogHeader className="border-b border-border px-5 py-4 text-left">
            <DialogTitle>搜尋分集「{folder_name}」</DialogTitle>
          </DialogHeader>

          <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
            {error && (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            )}

            {(phase === "scanning" || phase === "review" || phase === "done") && (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span>
                    已掃描資料夾 <strong>{foldersDone}</strong>
                    {phase === "scanning" && foldersQueued > 0 && (
                      <> / 待掃 {foldersQueued}</>
                    )}
                    ・看過 {filesSeen} 個檔案・找到 {items.length} 個分集
                  </span>
                  {phase === "scanning" && (
                    <span className="text-blue-300">掃描中…</span>
                  )}
                </div>
                {phase === "scanning" && <Progress value={scanPercent} />}
                {partial && (
                  <div className="flex items-start gap-1.5 rounded-md border border-amber-400/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                    <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                    掃描項目過多，僅處理部分檔案。請縮小範圍後再執行一次。
                  </div>
                )}
              </div>
            )}

            {(phase === "review" || phase === "done") && (
              <>
                {grouped.length === 0 ? (
                  <div className="rounded-md border border-border bg-muted/50 px-4 py-6 text-center text-sm text-muted-foreground">
                    沒有找到任何分集檔案
                  </div>
                ) : (
                  <>
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="text-muted-foreground">
                        已選 {selected.size} / {items.length} 個
                      </span>
                      <button
                        className="rounded border border-border px-2 py-0.5 hover:bg-muted"
                        onClick={selectAll}
                      >
                        全選
                      </button>
                      <button
                        className="rounded border border-border px-2 py-0.5 hover:bg-muted"
                        onClick={selectNone}
                      >
                        全不選
                      </button>
                      <button
                        className="rounded border border-border px-2 py-0.5 hover:bg-muted"
                        onClick={selectMultifileOnly}
                      >
                        只選「可能分集」
                      </button>
                      <button
                        className="rounded border border-border px-2 py-0.5 hover:bg-muted"
                        onClick={selectAllButLargestPerGroup}
                      >
                        每組保留最大檔
                      </button>
                    </div>

                    <ul className="max-h-[40vh] space-y-3 overflow-y-auto rounded-md border border-border bg-background/40 p-2 text-xs">
                      {grouped.map(({ code, eps }) => (
                        <li key={code} className="rounded border border-border/60 bg-muted/40 p-2">
                          <div className="mb-1 flex items-center gap-2 text-sm">
                            <span className="font-mono text-primary">{code}</span>
                            <span className="text-muted-foreground/70">({eps.length} 個)</span>
                          </div>
                          <ul className="space-y-0.5">
                            {eps.map((ep) => (
                              <li
                                key={ep.file_id}
                                className="flex items-baseline gap-2 py-0.5"
                              >
                                <input
                                  type="checkbox"
                                  checked={selected.has(ep.file_id)}
                                  onChange={() => toggle(ep.file_id)}
                                  className="h-3 w-3 accent-primary"
                                />
                                <span
                                  className={
                                    "rounded px-1.5 py-0.5 text-[10px] font-medium " +
                                    (ep.category === "canonical"
                                      ? "bg-emerald-400/20 text-emerald-200"
                                      : "bg-amber-400/20 text-amber-200")
                                  }
                                >
                                  {ep.category === "canonical"
                                    ? "已分集 _N"
                                    : "可能分集"}
                                </span>
                                <span className="truncate text-foreground/80">
                                  {ep.name}
                                </span>
                                <span className="ml-auto whitespace-nowrap text-muted-foreground/70">
                                  {fmtBytes(ep.size)}
                                </span>
                                <span className="ml-2 truncate font-mono text-[10px] text-muted-foreground/60">
                                  {ep.parent_path}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </li>
                      ))}
                    </ul>
                  </>
                )}

                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={autoStrip}
                    onChange={(e) => setAutoStrip(e.target.checked)}
                    className="accent-primary"
                  />
                  <span>刪除後自動去除剩餘 _N 標記</span>
                </label>
              </>
            )}

            {phase === "processing" && trashProgress && (
              <div className="space-y-1">
                <div className="text-xs text-muted-foreground">
                  刪除中… {trashProgress.current} / {trashProgress.total}
                </div>
                <Progress value={trashPercent} />
              </div>
            )}

            {stripEvents.length > 0 && (
              <div className="space-y-1 rounded-md border border-border bg-muted/50 px-3 py-2">
                <div className="text-xs text-muted-foreground">
                  去分集記錄 ({stripEvents.length})
                </div>
                <ul className="max-h-32 overflow-y-auto text-xs">
                  {stripEvents.slice(-20).map((e, i) => (
                    <li key={i} className="flex items-center gap-2">
                      {e.action === "rename" ? (
                        <Pencil className="h-3 w-3 shrink-0 text-emerald-300" aria-hidden />
                      ) : e.action === "skip" ? (
                        <SkipForward className="h-3 w-3 shrink-0 text-muted-foreground/70" aria-hidden />
                      ) : (
                        <X className="h-3 w-3 shrink-0 text-red-300" aria-hidden />
                      )}
                      <span className="truncate text-foreground/80">{e.source}</span>
                      <span className="text-muted-foreground/60">→</span>
                      <span className="truncate font-mono text-primary">
                        {e.target}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {summary && (
              <div className="space-y-1 rounded-md border border-border bg-muted/50 px-3 py-2 text-sm">
                <div className="flex items-center gap-1.5 text-red-300">
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                  刪除 {summary.trashed} 個
                </div>
                <div className="flex items-center gap-1.5 text-emerald-300">
                  <Pencil className="h-3.5 w-3.5" aria-hidden />
                  自動去分集 {summary.renamed} 個
                </div>
                {summary.skipped > 0 && (
                  <div className="flex items-center gap-1.5 text-muted-foreground">
                    <SkipForward className="h-3.5 w-3.5" aria-hidden />
                    略過 {summary.skipped} 個
                  </div>
                )}
              </div>
            )}

            {warnings.length > 0 && (
              <div className="max-h-24 space-y-1 overflow-y-auto rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                <div className="font-semibold">警告 ({warnings.length}):</div>
                {warnings.map((w, i) => (
                  <div key={i} className="truncate">• {w}</div>
                ))}
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2 border-t border-border px-5 py-4">
            {phase === "scanning" || phase === "processing" ? (
              <Button variant="ghost" onClick={cancel}>
                取消
              </Button>
            ) : phase === "review" ? (
              <>
                <Button variant="ghost" onClick={close}>
                  關閉
                </Button>
                <Button
                  onClick={runProcess}
                  disabled={!selected.size && !autoStrip}
                >
                  {selected.size
                    ? `移到垃圾桶 (${selected.size})`
                    : autoStrip
                    ? "只執行去分集"
                    : "移到垃圾桶"}
                </Button>
              </>
            ) : phase === "done" ? (
              <>
                <Button variant="ghost" onClick={close}>
                  關閉
                </Button>
                <Button onClick={startScan}>再次搜尋</Button>
              </>
            ) : (
              <Button variant="ghost" onClick={close}>
                關閉
              </Button>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
