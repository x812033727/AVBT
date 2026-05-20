"use client";

import { useRef, useState } from "react";
import { streamNdjson } from "@/lib/api";

type Progress = {
  current: number;
  kind: "file" | "folder";
  action: "move" | "rename" | "flatten" | "dedupe" | "skip" | "error";
  source: string;
  target: string | null;
  reason: string | null;
  section?: "migrate" | "cleanup";
  context?: string;
};

type Result = {
  total: number;
  moved: number;
  skipped: number;
  errors: number;
  source: string;
};

const ACTION_LABEL: Record<Progress["action"], { text: string; cls: string }> = {
  move: { text: "📦 搬移", cls: "text-emerald-300" },
  rename: { text: "✎ 改名", cls: "text-blue-300" },
  flatten: { text: "📤 攤平", cls: "text-emerald-300" },
  dedupe: { text: "🗑 去重", cls: "text-amber-300" },
  skip: { text: "⏭ 略過", cls: "text-white/40" },
  error: { text: "✗ 失敗", cls: "text-red-300" },
};

const REASON_LABEL: Record<string, string> = {
  no_code: "無法辨識番號",
  no_tracked_match: "尚未追蹤",
  already_in_place: "已在正確位置",
  already_clean: "已經正規化",
  bad_target: "目標路徑異常",
  duplicate: "重複",
};

export default function LegacySweepButton({
  archiveFolder,
  onDone,
}: {
  archiveFolder: string;
  onDone?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [total, setTotal] = useState(0);
  const [progress, setProgress] = useState<Progress[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [currentItem, setCurrentItem] = useState<string>("");
  const abortRef = useRef<AbortController | null>(null);

  async function submit() {
    setBusy(true);
    setResult(null);
    setProgress([]);
    setErrors([]);
    setTotal(0);
    setCurrentItem("");
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamNdjson(
        "/api/pikpak/archiver/sweep-legacy/stream",
        {},
        (event) => {
          if (event.type === "start") {
            setTotal(event.total ?? 0);
          } else if (event.type === "progress") {
            setProgress((prev) => [...prev, event]);
            setCurrentItem(event.source ?? "");
          } else if (event.type === "done") {
            setResult(event.result);
            setCurrentItem("");
          } else if (event.type === "error") {
            setErrors((prev) => [...prev, event.message ?? "未知錯誤"]);
          }
        },
        ctrl.signal
      );
      onDone?.();
    } catch (e: any) {
      if (e.name !== "AbortError") {
        setErrors((prev) => [...prev, e.message ?? String(e)]);
      }
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
    setErrors([]);
    setResult(null);
    setTotal(0);
    setCurrentItem("");
  }

  const percent = total ? Math.round((progress.length / total) * 100) : 0;
  const recent = progress.slice(-12).reverse();
  const counts = {
    move: progress.filter((p) => p.action === "move").length,
    rename: progress.filter((p) => p.action === "rename").length,
    flatten: progress.filter((p) => p.action === "flatten").length,
    skip: progress.filter((p) => p.action === "skip").length,
    error: progress.filter((p) => p.action === "error").length,
  };

  return (
    <>
      <button
        className="rounded border border-amber-400/40 bg-amber-500/10 px-2 py-0.5 text-amber-200 hover:bg-amber-500/20"
        onClick={() => setOpen(true)}
        title={`重新評估 ${archiveFolder}/ 內的番號,把新追蹤到的搬到對應分類資料夾`}
      >
        重新評估 已完成
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
          onClick={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div className="w-full max-w-2xl space-y-4 rounded-xl border border-white/10 bg-panel p-5">
            <div className="flex items-center">
              <h2 className="text-lg font-semibold">
                重新評估「{archiveFolder}」
              </h2>
              <button
                className="ml-auto text-white/40 hover:text-white"
                onClick={close}
                disabled={busy}
              >
                ✕
              </button>
            </div>

            <p className="text-xs text-white/50">
              掃描資料夾下所有番號,把新追蹤到的(系列/女優/...)搬到對應分類資料夾。
              進度即時顯示,卡住時可以看到目前處理到哪個檔案。
            </p>

            {(busy || result || progress.length > 0) && (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-white/60">
                  <span>
                    {progress.length} / {total || "?"}
                    {total > 0 && ` (${percent}%)`}
                  </span>
                  <span className="flex gap-2">
                    <span className="text-emerald-300">搬 {counts.move}</span>
                    <span className="text-blue-300">改名 {counts.rename}</span>
                    <span className="text-emerald-300">攤平 {counts.flatten}</span>
                    <span className="text-white/40">略過 {counts.skip}</span>
                    {counts.error > 0 && (
                      <span className="text-red-300">失敗 {counts.error}</span>
                    )}
                  </span>
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
                    <li className="text-white/40">等待第一筆…</li>
                  )}
                  {recent.map((p) => {
                    const lbl = ACTION_LABEL[p.action] ?? {
                      text: p.action,
                      cls: "text-white/60",
                    };
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

            {errors.length > 0 && (
              <div className="max-h-32 space-y-1 overflow-y-auto rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                <div className="font-semibold">錯誤 ({errors.length}):</div>
                {errors.map((msg, i) => (
                  <div key={i} className="font-mono">
                    • {msg}
                  </div>
                ))}
              </div>
            )}

            {result && (
              <div className="space-y-1 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
                <div>
                  共 <strong>{result.total}</strong> 個項目
                </div>
                <div className="text-emerald-300">
                  📦 搬移 / 改名 {result.moved}
                </div>
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
                    {result ? "再執行一次" : "開始"}
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
