"use client";

import { useRef, useState } from "react";
import {
  File,
  FileOutput,
  Folder,
  Paintbrush,
  PenLine,
  SkipForward,
  X,
  type LucideIcon,
} from "lucide-react";
import { ErrorBox } from "@/components/shared/ErrorBox";
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
import { streamNdjson } from "@/lib/api";

type Progress = {
  current: number;
  kind: "file" | "folder";
  action: "rename" | "flatten" | "skip" | "error";
  source: string;
  target: string | null;
  reason: string | null;
};

type Result = {
  total: number;
  renamed: number;
  flattened: number;
  skipped: number;
  errors: number;
  dry_run: boolean;
};

const ACTION_LABEL: Record<
  Progress["action"],
  { icon: LucideIcon; text: string; cls: string }
> = {
  rename: { icon: PenLine, text: "改名", cls: "text-blue-300" },
  flatten: { icon: FileOutput, text: "取出主檔", cls: "text-sky-300" },
  skip: { icon: SkipForward, text: "略過", cls: "text-muted-foreground/70" },
  error: { icon: X, text: "失敗", cls: "text-red-300" },
};

const REASON_LABEL: Record<string, string> = {
  no_code: "無法辨識番號",
  already_clean: "已經正規化",
  conflict: "同名衝突",
};

export default function PCloudCleanupButton({
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
        "/api/pcloud/files/cleanup/stream",
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

  // A wrapper folder can fan out into several extraction events, so
  // progress.length may exceed total (direct children) — clamp at 100%.
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
            : "把此資料夾下的 BT 髒名字正規化為 <番號>.ext"
        }
      >
        <Paintbrush aria-hidden />
        整理此資料夾
      </Button>

      <Dialog
        open={open}
        onOpenChange={(v) => {
          if (!v) close();
        }}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>整理「{folder_name}」</DialogTitle>
          </DialogHeader>

          <p className="text-xs text-muted-foreground">
            整理此資料夾的內容(就地,不分類)。檔案改名為{" "}
            <span className="font-mono">番號.ext</span>;子資料夾會「鑽進去」
            (遞迴最多 6 層)把主影片取出到這層、改名{" "}
            <span className="font-mono">番號.ext</span>,再把空殼包裝資料夾
            (含 sample / nfo / 種子)送進回收桶。
            {" "}
            <span className="text-amber-300/70">
              只有整支大檔都成功取出才會刪資料夾,取不出來的會原封不動保留
            </span>
            。要再依番號分到 <span className="font-mono">AVBT/&lt;類別&gt;/</span>{" "}
            請改用「歸類」。
          </p>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={dryRun}
              onCheckedChange={(v) => setDryRun(v === true)}
              disabled={busy}
            />
            <span>只預覽（不實際修改）</span>
          </label>

          {error && <ErrorBox message={error} />}

          {(busy || result) && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  已處理 {progress.length}
                  {total > 0 ? ` / 共 ${total} 項 (${percent}%)` : ""}
                  {result?.dry_run && " ・ 預覽模式"}
                </span>
                <span>
                  改名 {progress.filter((p) => p.action === "rename").length} ／
                  取出 {progress.filter((p) => p.action === "flatten").length} ／
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
                  const ActionIcon = lbl.icon;
                  const reasonTxt =
                    p.reason && REASON_LABEL[p.reason]
                      ? `（${REASON_LABEL[p.reason]}）`
                      : p.reason
                      ? `（${p.reason}）`
                      : "";
                  return (
                    <li
                      key={p.current}
                      className="flex items-baseline gap-2 py-0.5"
                    >
                      <span
                        className={`inline-flex items-center gap-1 ${lbl.cls}`}
                      >
                        <ActionIcon className="h-3 w-3 shrink-0" aria-hidden />
                        {lbl.text}
                        {reasonTxt}
                      </span>
                      <span className="inline-flex min-w-0 items-center gap-1 truncate text-muted-foreground">
                        {p.kind === "folder" ? (
                          <Folder className="h-3 w-3 shrink-0" aria-hidden />
                        ) : (
                          <File className="h-3 w-3 shrink-0" aria-hidden />
                        )}
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
                  <span className="ml-2 text-amber-300/80">
                    （僅預覽，未修改）
                  </span>
                )}
              </div>
              <div className="flex items-center gap-1 text-blue-300">
                <PenLine className="h-3.5 w-3.5" aria-hidden />
                改名 {result.renamed}
              </div>
              {result.flattened > 0 && (
                <div className="flex items-center gap-1 text-sky-300">
                  <FileOutput className="h-3.5 w-3.5" aria-hidden />
                  取出主檔 {result.flattened}
                </div>
              )}
              <div className="flex items-center gap-1 text-muted-foreground">
                <SkipForward className="h-3.5 w-3.5" aria-hidden />
                略過 {result.skipped}
              </div>
              {result.errors > 0 && (
                <div className="flex items-center gap-1 text-red-300">
                  <X className="h-3.5 w-3.5" aria-hidden />
                  失敗 {result.errors}
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            {busy ? (
              <Button variant="ghost" size="sm" onClick={cancel}>
                取消
              </Button>
            ) : (
              <>
                <Button variant="ghost" size="sm" onClick={close}>
                  關閉
                </Button>
                <Button size="sm" onClick={submit}>
                  {dryRun ? "預覽" : "執行"}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
