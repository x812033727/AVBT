"use client";

import { useRef, useState } from "react";
import { streamNdjson } from "@/lib/api";

type Options = {
  uncensored: boolean;
  max_pages: number;
  hd_only: boolean;
  subtitle_only: boolean;
  skip_sent: boolean;
  min_size_mb: number | null;
  max_size_mb: number | null;
};

type Result = {
  total_movies: number;
  sent: number;
  skipped_no_magnet: number;
  skipped_already_sent: number;
  failed: number;
  errors: string[];
};

type Progress = {
  current: number;
  code: string;
  status: string;
  magnet_name?: string;
  message?: string;
};

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  sent: { text: "✓ 已送", cls: "text-emerald-300" },
  skipped_no_magnet: { text: "⏭ 無磁力", cls: "text-white/50" },
  skipped_already_sent: { text: "⏭ 已送過", cls: "text-white/50" },
  failed: { text: "✗ 失敗", cls: "text-red-300" },
};

const DEFAULT_OPTIONS: Options = {
  uncensored: false,
  max_pages: 5,
  hd_only: true,
  subtitle_only: false,
  skip_sent: true,
  min_size_mb: null,
  max_size_mb: null,
};

export default function BulkSendButton({
  streamPath,
  title,
  buttonLabel = "送全部到 PikPak",
  showMaxPages = true,
  defaultOptions,
  extraBody,
  onDone,
  disabled,
}: {
  streamPath: string;
  title: string;
  buttonLabel?: string;
  showMaxPages?: boolean;
  defaultOptions?: Partial<Options>;
  /** Extra fields merged into the POST body (e.g. {codes: [...]}). */
  extraBody?: Record<string, any>;
  /** Called after the stream finishes (success or cancel). */
  onDone?: (result: Result | null) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [total, setTotal] = useState<number>(0);
  const [progress, setProgress] = useState<Progress[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opts, setOpts] = useState<Options>({
    ...DEFAULT_OPTIONS,
    ...defaultOptions,
  });
  const abortRef = useRef<AbortController | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    setResult(null);
    setProgress([]);
    setTotal(0);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let finalResult: Result | null = null;
    try {
      await streamNdjson(
        streamPath,
        { ...opts, ...defaultOptions, ...extraBody },
        (event) => {
          if (event.type === "start") setTotal(event.total ?? 0);
          else if (event.type === "progress")
            setProgress((prev) => [...prev, event]);
          else if (event.type === "done") {
            setResult(event.result);
            finalResult = event.result;
          } else if (event.type === "error") setError(event.message);
        },
        ctrl.signal
      );
    } catch (e: any) {
      if (e.name !== "AbortError") setError(e.message);
    } finally {
      setBusy(false);
      abortRef.current = null;
      onDone?.(finalResult);
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
  }

  const done = result !== null;
  const percent = total ? Math.round((progress.length / total) * 100) : 0;
  const recent = progress.slice(-8).reverse();

  return (
    <>
      <button
        className="btn-primary disabled:opacity-50"
        onClick={() => setOpen(true)}
        disabled={disabled}
      >
        {buttonLabel}
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
          onClick={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div className="w-full max-w-lg space-y-4 rounded-xl border border-white/10 bg-panel p-5">
            <div className="flex items-center">
              <h2 className="text-lg font-semibold">{title}</h2>
              <button
                className="ml-auto text-white/40 hover:text-white"
                onClick={close}
              >
                ✕
              </button>
            </div>

            {!busy && !done && (
              <div className="space-y-3 text-sm">
                {showMaxPages && (
                  <label className="flex items-center justify-between">
                    <span className="text-white/70">最多抓幾頁</span>
                    <input
                      type="number"
                      min={1}
                      max={20}
                      value={opts.max_pages}
                      onChange={(e) =>
                        setOpts({
                          ...opts,
                          max_pages: parseInt(e.target.value || "1"),
                        })
                      }
                      className="w-20 rounded-md border border-white/10 bg-ink px-2 py-1 text-right"
                    />
                  </label>
                )}
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={opts.hd_only}
                    onChange={(e) =>
                      setOpts({ ...opts, hd_only: e.target.checked })
                    }
                  />
                  <span>優先高清</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={opts.subtitle_only}
                    onChange={(e) =>
                      setOpts({ ...opts, subtitle_only: e.target.checked })
                    }
                  />
                  <span>優先有字幕</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={opts.skip_sent}
                    onChange={(e) =>
                      setOpts({ ...opts, skip_sent: e.target.checked })
                    }
                  />
                  <span>跳過已送過的</span>
                </label>
                <div className="flex items-center justify-between">
                  <span className="text-white/70">檔案大小 (MB)</span>
                  <div className="flex items-center gap-1 text-xs">
                    <input
                      type="number"
                      min={0}
                      placeholder="不限"
                      value={opts.min_size_mb ?? ""}
                      onChange={(e) =>
                        setOpts({
                          ...opts,
                          min_size_mb: e.target.value
                            ? parseFloat(e.target.value)
                            : null,
                        })
                      }
                      className="w-20 rounded-md border border-white/10 bg-ink px-2 py-1 text-right"
                    />
                    <span>~</span>
                    <input
                      type="number"
                      min={0}
                      placeholder="不限"
                      value={opts.max_size_mb ?? ""}
                      onChange={(e) =>
                        setOpts({
                          ...opts,
                          max_size_mb: e.target.value
                            ? parseFloat(e.target.value)
                            : null,
                        })
                      }
                      className="w-20 rounded-md border border-white/10 bg-ink px-2 py-1 text-right"
                    />
                  </div>
                </div>
                <p className="text-xs text-white/40">
                  範圍外或不在範圍內的磁力會跳過；大小未標示的磁力不會被過濾。
                </p>
              </div>
            )}

            {error && (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            )}

            {(busy || done) && total > 0 && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs text-white/60">
                  <span>
                    {progress.length} / {total} ({percent}%)
                  </span>
                  <span>
                    送 {progress.filter((p) => p.status === "sent").length} ／
                    跳過{" "}
                    {progress.filter((p) => p.status.startsWith("skipped")).length}{" "}
                    ／ 失敗{" "}
                    {progress.filter((p) => p.status === "failed").length}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded bg-white/10">
                  <div
                    className="h-full bg-accent transition-[width]"
                    style={{ width: `${percent}%` }}
                  />
                </div>
                <ul className="max-h-48 overflow-y-auto rounded-md border border-white/10 bg-ink/50 p-2 text-xs">
                  {recent.length === 0 && (
                    <li className="text-white/40">等待第一筆…</li>
                  )}
                  {recent.map((p) => {
                    const label = STATUS_LABEL[p.status] ?? {
                      text: p.status,
                      cls: "text-white/60",
                    };
                    return (
                      <li
                        key={p.current}
                        className="flex items-baseline gap-2 py-0.5"
                      >
                        <span className="font-mono text-accent">{p.code}</span>
                        <span className={label.cls}>{label.text}</span>
                        {p.magnet_name && (
                          <span className="truncate text-white/40">
                            {p.magnet_name}
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {done && result && (
              <div className="space-y-1 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
                <div>
                  共 <strong>{result.total_movies}</strong> 部
                </div>
                <div className="text-emerald-300">✓ 已送 {result.sent}</div>
                <div className="text-white/60">
                  ⏭ 跳過 (無磁力 {result.skipped_no_magnet}, 已送過{" "}
                  {result.skipped_already_sent})
                </div>
                {result.failed > 0 && (
                  <div className="text-red-300">✗ 失敗 {result.failed}</div>
                )}
                {result.errors.length > 0 && (
                  <details className="text-xs text-white/50">
                    <summary>錯誤明細 ({result.errors.length})</summary>
                    <ul className="mt-1 space-y-0.5">
                      {result.errors.slice(0, 20).map((e, i) => (
                        <li key={i} className="break-all">
                          • {e}
                        </li>
                      ))}
                    </ul>
                  </details>
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
                  {!done && (
                    <button className="btn-primary" onClick={submit}>
                      開始
                    </button>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
