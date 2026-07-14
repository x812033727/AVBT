"use client";

import { useRef, useState } from "react";
import {
  FileText,
  Folder,
  FolderInput,
  FolderOutput,
  Pencil,
  SkipForward,
  Sparkles,
  Trash2,
  X,
  type LucideIcon,
} from "lucide-react";
import { streamNdjson } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress as ProgressBar } from "@/components/ui/progress";

type Progress = {
  current: number;
  kind: "file" | "folder";
  action: "rename" | "flatten" | "move" | "trash" | "skip" | "error";
  source: string;
  target: string | null;
  reason: string | null;
  nested_in?: string;
};

type Result = {
  total: number;
  renamed: number;
  flattened: number;
  moved: number;
  skipped: number;
  trashed: number;
  errors: number;
  dry_run: boolean;
};

const ACTION_LABEL: Record<
  Progress["action"],
  { icon: LucideIcon; text: string; cls: string }
> = {
  rename: { icon: Pencil, text: "改名", cls: "text-blue-300" },
  flatten: { icon: FolderOutput, text: "攤平", cls: "text-emerald-300" },
  move: { icon: FolderInput, text: "搬移", cls: "text-violet-300" },
  trash: { icon: Trash2, text: "刪除空資料夾", cls: "text-orange-300" },
  skip: { icon: SkipForward, text: "略過", cls: "text-muted-foreground" },
  error: { icon: X, text: "失敗", cls: "text-red-300" },
};

const REASON_LABEL: Record<string, string> = {
  no_code: "無法辨識番號",
  already_clean: "已經正規化",
  conflict: "同名衝突",
  transferring: "檔案傳輸中，下輪再處理",
};

export default function CleanupButton({
  folder_id,
  folder_name,
  onDone,
  disabled,
}: {
  folder_id: string;
  folder_name: string;
  onDone?: () => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [total, setTotal] = useState(0);
  const [progress, setProgress] = useState<Progress[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    setResult(null);
    setProgress([]);
    setTotal(0);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const wasDryRun = dryRun;
    try {
      await streamNdjson(
        "/api/pikpak/files/cleanup/stream",
        { folder_id, dry_run: wasDryRun },
        (event) => {
          if (event.type === "start") setTotal(event.total ?? 0);
          else if (event.type === "progress")
            setProgress((prev) => [...prev, event]);
          else if (event.type === "done") setResult(event.result);
          else if (event.type === "error") setError(event.message);
        },
        ctrl.signal
      );
      if (!wasDryRun) onDone?.();
    } catch (e: any) {
      if (e.name !== "AbortError") setError(e.message);
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  function cancel() {
    abortRef.current?.abort();
  }

  function close() {
    if (busy) return;
    setOpen(false);
    setProgress([]);
    setResult(null);
    setError(null);
    setTotal(0);
    setDryRun(true);
  }

  // ``total`` is only the top-level child count; recursion discovers more
  // items as it descends, so treat it as a lower bound and clamp.
  const percent = total
    ? Math.min(100, Math.round((progress.length / total) * 100))
    : 0;
  const recent = progress.slice(-10).reverse();

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setOpen(true)}
        disabled={disabled}
        title={
          disabled
            ? "根目錄不可整理，請先進入子資料夾"
            : "遞迴整理：搬到正確製作商/系列、正規化、刪空資料夾"
        }
      >
        <Sparkles aria-hidden />
        整理此資料夾
      </Button>

      <Dialog open={open} onOpenChange={(o) => !o && close()}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>整理「{folder_name}」</DialogTitle>
          </DialogHeader>

          <p className="text-xs text-muted-foreground">
            會遞迴進入分類子資料夾（製作商／廠商／系列）找出番號，把放錯位置的番號搬到正確的{" "}
            <span className="font-mono">製作商/&lt;studio&gt;/&lt;系列&gt;/&lt;番號&gt;</span>
            ，再就地正規化／攤平；搬空後的資料夾會移到垃圾桶（PikPak 可還原約 30 天）。
            不會刪除仍有內容的資料夾或此根目錄。建議先勾選預覽，且勿在全域整理進行中同時執行。
          </p>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={dryRun}
              onCheckedChange={(v) => setDryRun(v === true)}
              disabled={busy}
            />
            <span>只預覽（不實際修改）</span>
          </label>

          {error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
              {error}
            </div>
          )}

          {(busy || result) && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  {progress.length} / {total} ({percent}%)
                  {result?.dry_run && " ・ 預覽模式"}
                </span>
                <span>
                  搬移 {progress.filter((p) => p.action === "move").length} ／
                  攤平 {progress.filter((p) => p.action === "flatten").length} ／
                  改名 {progress.filter((p) => p.action === "rename").length} ／
                  刪空夾 {progress.filter((p) => p.action === "trash").length} ／
                  略過 {progress.filter((p) => p.action === "skip").length} ／
                  失敗 {progress.filter((p) => p.action === "error").length}
                </span>
              </div>
              <ProgressBar value={percent} className="h-2" />
              <ul className="max-h-56 overflow-y-auto rounded-md border border-border bg-background/50 p-2 text-xs">
                {recent.length === 0 && (
                  <li className="text-muted-foreground/70">等待第一筆…</li>
                )}
                {recent.map((p) => {
                  const lbl = ACTION_LABEL[p.action];
                  const KindIcon = p.kind === "folder" ? Folder : FileText;
                  const reasonTxt =
                    p.reason && REASON_LABEL[p.reason]
                      ? `（${REASON_LABEL[p.reason]}）`
                      : p.reason
                      ? `（${p.reason}）`
                      : "";
                  return (
                    <li
                      key={p.current}
                      className="flex items-center gap-2 py-0.5"
                    >
                      <span className={`inline-flex shrink-0 items-center gap-1 ${lbl.cls}`}>
                        <lbl.icon className="h-3 w-3" aria-hidden />
                        {lbl.text}
                        {reasonTxt}
                      </span>
                      <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground">
                        <KindIcon className="h-3 w-3 shrink-0" aria-hidden />
                        <span className="truncate">{p.source}</span>
                      </span>
                      {p.target && p.target !== p.source && (
                        <>
                          <span className="text-muted-foreground/40">→</span>
                          <span className="truncate font-mono text-primary">
                            {p.target}
                          </span>
                        </>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {result && (
            <div className="space-y-1 rounded-md border border-border bg-card px-3 py-2 text-sm">
              <div>
                共 <strong>{result.total}</strong> 個項目
                {result.dry_run && (
                  <span className="ml-2 text-amber-300/80">（僅預覽，未修改）</span>
                )}
              </div>
              <div className="text-violet-300">搬移 {result.moved}</div>
              <div className="text-emerald-300">攤平 {result.flattened}</div>
              <div className="text-blue-300">改名 {result.renamed}</div>
              {result.trashed > 0 && (
                <div className="text-orange-300">刪除空資料夾 {result.trashed}</div>
              )}
              <div className="text-muted-foreground">略過 {result.skipped}</div>
              {result.errors > 0 && (
                <div className="text-red-300">失敗 {result.errors}</div>
              )}
            </div>
          )}

          <DialogFooter>
            {busy ? (
              <Button variant="ghost" onClick={cancel}>
                取消
              </Button>
            ) : (
              <>
                <Button variant="ghost" onClick={close}>
                  關閉
                </Button>
                <Button onClick={submit}>{dryRun ? "預覽" : "執行"}</Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
