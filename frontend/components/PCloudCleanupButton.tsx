"use client";

import { useRef, useState } from "react";
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

const ACTION_LABEL: Record<Progress["action"], { text: string; cls: string }> = {
  rename: { text: "✎ 改名", cls: "text-blue-300" },
  flatten: { text: "📤 取出主檔", cls: "text-sky-300" },
  skip: { text: "⏭ 略過", cls: "text-white/50" },
  error: { text: "✗ 失敗", cls: "text-red-300" },
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
      <button
        className="btn-ghost disabled:opacity-30"
        onClick={() => setOpen(true)}
        disabled={disabled}
        title={
          disabled
            ? "根目錄不可整理，請先進入子資料夾"
            : "把此資料夾下的 BT 髒名字正規化為 <番號>.ext"
        }
      >
        🧹 整理此資料夾
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
          onClick={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div className="w-full max-w-xl space-y-4 rounded-xl border border-white/10 bg-panel p-5">
            <div className="flex items-center">
              <h2 className="text-lg font-semibold">
                整理「{folder_name}」
              </h2>
              <button
                className="ml-auto text-white/40 hover:text-white"
                onClick={close}
              >
                ✕
              </button>
            </div>

            <p className="text-xs text-white/50">
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
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
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
                <div className="flex items-center justify-between text-xs text-white/60">
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
                <div className="h-2 overflow-hidden rounded bg-white/10">
                  <div
                    className="h-full bg-accent transition-[width]"
                    style={{ width: `${percent}%` }}
                  />
                </div>
                <ul className="max-h-56 overflow-y-auto rounded-md border border-white/10 bg-ink/50 p-2 text-xs">
                  {recent.length === 0 && (
                    <li className="text-white/40">等待第一筆…</li>
                  )}
                  {recent.map((p) => {
                    const lbl = ACTION_LABEL[p.action];
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
                        <span className={lbl.cls}>
                          {lbl.text}
                          {reasonTxt}
                        </span>
                        <span className="truncate text-white/60">
                          {p.kind === "folder" ? "📁 " : "📄 "}
                          {p.source}
                        </span>
                        {p.target && p.target !== p.source && (
                          <>
                            <span className="text-white/30">→</span>
                            <span className="truncate font-mono text-accent">
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
              <div className="space-y-1 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
                <div>
                  共 <strong>{result.total}</strong> 個項目
                  {result.dry_run && (
                    <span className="ml-2 text-amber-300/80">（僅預覽，未修改）</span>
                  )}
                </div>
                <div className="text-blue-300">✎ 改名 {result.renamed}</div>
                {result.flattened > 0 && (
                  <div className="text-sky-300">📤 取出主檔 {result.flattened}</div>
                )}
                <div className="text-white/60">⏭ 略過 {result.skipped}</div>
                {result.errors > 0 && (
                  <div className="text-red-300">✗ 失敗 {result.errors}</div>
                )}
              </div>
            )}

            <div className="flex justify-end gap-2">
              {busy ? (
                <button className="btn-ghost" onClick={cancel}>
                  取消
                </button>
              ) : (
                <>
                  <button className="btn-ghost" onClick={close}>
                    關閉
                  </button>
                  <button className="btn-primary" onClick={submit}>
                    {dryRun ? "預覽" : "執行"}
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
