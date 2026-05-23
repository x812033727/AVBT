"use client";

import { useRef, useState } from "react";
import { streamNdjson } from "@/lib/api";

type Progress = {
  current: number;
  kind: "file" | "folder";
  action: "move" | "skip" | "error";
  source: string;
  code?: string | null;
  listing_kind?: string | null;
  listing_name?: string | null;
  target_path?: string | null;
  target_name?: string | null;
  would_create?: boolean;
  reason?: string | null;
};

type Result = {
  total: number;
  moved: number;
  skipped: number;
  errors: number;
  dry_run: boolean;
};

const ACTION_LABEL: Record<Progress["action"], { text: string; cls: string }> = {
  move: { text: "📦 歸類", cls: "text-emerald-300" },
  skip: { text: "⏭ 略過", cls: "text-white/50" },
  error: { text: "✗ 失敗", cls: "text-red-300" },
};

const REASON_LABEL: Record<string, string> = {
  no_code: "無法辨識番號",
  no_tracked_match: "無追蹤對應",
  already_organized: "已在目標資料夾",
};

const KIND_LABEL: Record<string, string> = {
  series: "系列",
  star: "女優",
  studio: "製作商",
  label: "發行商",
  director: "導演",
};

export default function PCloudOrganizeButton({
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
  const [processing, setProcessing] = useState<{ current: number; source: string } | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    setResult(null);
    setProgress([]);
    setProcessing(null);
    setTotal(0);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const wasDryRun = dryRun;
    try {
      await streamNdjson(
        "/api/pcloud/files/organize/stream",
        { folder_id, dry_run: wasDryRun },
        (event) => {
          if (event.type === "start") setTotal(event.total ?? 0);
          else if (event.type === "processing")
            setProcessing({ current: event.current, source: event.source });
          else if (event.type === "progress") {
            setProgress((prev) => [...prev, event]);
            setProcessing(null);
          } else if (event.type === "done") {
            setResult(event.result);
            setProcessing(null);
          } else if (event.type === "error") {
            setError(event.message);
            setProcessing(null);
          }
        },
        ctrl.signal
      );
      if (!wasDryRun) onDone?.();
    } catch (e: any) {
      if (e.name !== "AbortError") setError(e.message);
    } finally {
      setBusy(false);
      setProcessing(null);
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
    setProcessing(null);
    setResult(null);
    setError(null);
    setTotal(0);
    setDryRun(true);
  }

  const percent = total ? Math.round((progress.length / total) * 100) : 0;
  const recent = progress.slice(-10).reverse();

  return (
    <>
      <button
        className="btn-ghost disabled:opacity-30"
        onClick={() => setOpen(true)}
        disabled={disabled}
        title={
          disabled
            ? "根目錄不可歸類，請先進入子資料夾"
            : "依番號自動搬到 AVBT/<系列>/<追蹤名稱>/ 之下"
        }
      >
        📦 歸類此資料夾
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
                歸類「{folder_name}」
              </h2>
              <button
                className="ml-auto text-white/40 hover:text-white"
                onClick={close}
              >
                ✕
              </button>
            </div>

            <p className="text-xs text-white/50">
              只動此資料夾的直接子項目。對每個有番號的影片 / 資料夾，依 JavBus + 追蹤清單反查到所屬系列 / 女優 / 製作商，搬到{" "}
              <span className="font-mono">AVBT/&lt;類別&gt;/&lt;追蹤名&gt;/</span>。沒對應追蹤項目的會略過。
            </p>

            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
                disabled={busy}
              />
              <span>只預覽（不實際修改，也不建立目標資料夾）</span>
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
                    {progress.length} / {total} ({percent}%)
                    {result?.dry_run && " ・ 預覽模式"}
                  </span>
                  <span>
                    歸類 {progress.filter((p) => p.action === "move").length} ／
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
                {processing && (
                  <div className="flex items-center gap-2 rounded-md border border-amber-400/20 bg-amber-400/5 px-2 py-1 text-xs text-amber-200/80">
                    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-amber-400" />
                    <span>
                      ⏳ 正在查 JavBus（{processing.current}/{total}）：
                    </span>
                    <span className="truncate font-mono">{processing.source}</span>
                  </div>
                )}
                <ul className="max-h-72 overflow-y-auto rounded-md border border-white/10 bg-ink/50 p-2 text-xs">
                  {recent.length === 0 && !processing && (
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
                    const kindTag = p.listing_kind
                      ? KIND_LABEL[p.listing_kind] || p.listing_kind
                      : null;
                    return (
                      <li
                        key={p.current}
                        className="flex flex-col gap-0.5 py-0.5"
                      >
                        <div className="flex items-baseline gap-2">
                          <span className={lbl.cls}>
                            {lbl.text}
                            {reasonTxt}
                          </span>
                          <span className="truncate text-white/60">
                            {p.kind === "folder" ? "📁 " : "📄 "}
                            {p.source}
                          </span>
                        </div>
                        {p.action === "move" && p.target_path && (
                          <div className="ml-8 flex items-baseline gap-1 text-white/50">
                            <span className="text-white/30">→</span>
                            {kindTag && (
                              <span className="rounded bg-emerald-500/10 px-1 text-[10px] text-emerald-300">
                                {kindTag}
                              </span>
                            )}
                            <span className="truncate font-mono text-accent">
                              {p.target_path}
                              {p.target_name ? `/${p.target_name}` : ""}
                            </span>
                            {p.would_create && (
                              <span className="text-[10px] text-amber-300/80">
                                （將建立）
                              </span>
                            )}
                          </div>
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
                <div className="text-emerald-300">📦 已歸類 {result.moved}</div>
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
