"use client";

import { useRef, useState } from "react";
import {
  FileText,
  Flame,
  Folder,
  FolderInput,
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
  action: "rename" | "move" | "purge" | "trash" | "skip" | "error";
  source: string;
  target: string | null;
  reason: string | null;
};

type Result = {
  kept: number;
  renamed: number;
  moved: number;
  purged: number;
  trashed: number;
  skipped: number;
  errors: number;
  dry_run: boolean;
  no_video?: boolean;
};

const ACTION_LABEL: Record<
  Progress["action"],
  { icon: LucideIcon; text: string; cls: string }
> = {
  rename: { icon: Pencil, text: "改名", cls: "text-blue-300" },
  move: { icon: FolderInput, text: "搬到番號夾", cls: "text-violet-300" },
  purge: { icon: Flame, text: "永久刪除", cls: "text-red-300" },
  trash: { icon: Trash2, text: "丟垃圾桶", cls: "text-orange-300" },
  skip: { icon: SkipForward, text: "略過", cls: "text-muted-foreground" },
  error: { icon: X, text: "失敗", cls: "text-red-300" },
};

const REASON_LABEL: Record<string, string> = {
  duplicate: "重複影片",
  not_empty: "夾內仍有檔案",
};

export default function FinalizeButton({
  code,
  onDone,
}: {
  code: string;
  onDone?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [total, setTotal] = useState(0);
  const [progress, setProgress] = useState<Progress[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [warn, setWarn] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    setWarn(null);
    setResult(null);
    setProgress([]);
    setTotal(0);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const wasDryRun = dryRun;
    try {
      await streamNdjson(
        "/api/pikpak/finalize/stream",
        { code, dry_run: wasDryRun },
        (event) => {
          if (event.type === "start") setTotal(event.total ?? 0);
          else if (event.type === "progress")
            setProgress((prev) => [...prev, event]);
          else if (event.type === "warn") setWarn(event.message);
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
    setWarn(null);
    setError(null);
    setTotal(0);
    setDryRun(true);
  }

  const percent = total
    ? Math.min(100, Math.round((progress.length / total) * 100))
    : 0;
  const recent = progress.slice(-10).reverse();

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 text-xs text-violet-300 hover:underline"
        title="只保留影片並正名,永久刪除廣告/文字檔/截圖"
      >
        <Sparkles className="h-3 w-3" aria-hidden />
        整理
      </button>

      <Dialog open={open} onOpenChange={(o) => !o && close()}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>整理「{code}」歸檔資料夾</DialogTitle>
          </DialogHeader>

          <p className="text-xs text-muted-foreground">
            只保留實際影片並正名為{" "}
            <span className="font-mono">{code}.ext</span>（分集為{" "}
            <span className="font-mono">{code}_1.ext</span>、
            <span className="font-mono">{code}_2.ext</span>…）。
            廣告短片、文字檔、截圖、Sample 資料夾將
            <span className="font-semibold text-red-300">
              永久刪除（不可還原）
            </span>
            ；疑似重複的影片只丟垃圾桶（可還原約 30 天）。建議先勾選預覽。
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
          {warn && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
              {warn}
            </div>
          )}

          {(busy || result) && !warn && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  {progress.length} / {total} ({percent}%)
                  {result?.dry_run && " ・ 預覽模式"}
                </span>
                <span>
                  改名 {progress.filter((p) => p.action === "rename").length} ／
                  搬移 {progress.filter((p) => p.action === "move").length} ／
                  永久刪 {progress.filter((p) => p.action === "purge").length} ／
                  垃圾桶 {progress.filter((p) => p.action === "trash").length} ／
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
                      <span
                        className={`inline-flex shrink-0 items-center gap-1 ${lbl.cls}`}
                      >
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

          {result && !result.no_video && (
            <div className="space-y-1 rounded-md border border-border bg-card px-3 py-2 text-sm">
              <div>
                保留 <strong>{result.kept}</strong> 部影片
                {result.dry_run && (
                  <span className="ml-2 text-amber-300/80">
                    （僅預覽，未修改）
                  </span>
                )}
              </div>
              <div className="text-blue-300">改名 {result.renamed}</div>
              <div className="text-violet-300">搬移 {result.moved}</div>
              <div className="text-red-300">永久刪除 {result.purged}</div>
              {result.trashed > 0 && (
                <div className="text-orange-300">丟垃圾桶 {result.trashed}</div>
              )}
              {result.skipped > 0 && (
                <div className="text-muted-foreground">略過 {result.skipped}</div>
              )}
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
