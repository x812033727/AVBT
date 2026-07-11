"use client";

import { useId, useRef, useState } from "react";
import { Check, SkipForward, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress as ProgressBar } from "@/components/ui/progress";
import { streamNdjson } from "@/lib/api";
import { cn } from "@/lib/utils";

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

const STATUS_LABEL: Record<
  string,
  { text: string; icon: LucideIcon; cls: string }
> = {
  sent: { text: "已送", icon: Check, cls: "text-emerald-300" },
  skipped_no_magnet: {
    text: "無磁力",
    icon: SkipForward,
    cls: "text-muted-foreground",
  },
  skipped_already_sent: {
    text: "已送過",
    icon: SkipForward,
    cls: "text-muted-foreground",
  },
  failed: { text: "失敗", icon: X, cls: "text-red-300" },
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
  const uid = useId();
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
      <Button onClick={() => setOpen(true)} disabled={disabled}>
        {buttonLabel}
      </Button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
          onClick={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div className="w-full max-w-lg space-y-4 rounded-lg border border-border bg-card p-5">
            <div className="flex items-center">
              <h2 className="text-lg font-semibold">{title}</h2>
              <button
                type="button"
                className="ml-auto rounded-md p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
                onClick={close}
                aria-label="關閉"
              >
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>

            {!busy && !done && (
              <div className="space-y-3 text-sm">
                {showMaxPages && (
                  <div className="flex items-center justify-between">
                    <Label
                      htmlFor={`${uid}-max-pages`}
                      className="font-normal text-muted-foreground"
                    >
                      最多抓幾頁
                    </Label>
                    <Input
                      id={`${uid}-max-pages`}
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
                      className="h-8 w-20 px-2 text-right"
                    />
                  </div>
                )}
                <div className="flex items-center gap-2">
                  <Checkbox
                    id={`${uid}-hd-only`}
                    checked={opts.hd_only}
                    onCheckedChange={(v) =>
                      setOpts({ ...opts, hd_only: v === true })
                    }
                  />
                  <Label htmlFor={`${uid}-hd-only`} className="font-normal">
                    優先高清
                  </Label>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id={`${uid}-subtitle-only`}
                    checked={opts.subtitle_only}
                    onCheckedChange={(v) =>
                      setOpts({ ...opts, subtitle_only: v === true })
                    }
                  />
                  <Label
                    htmlFor={`${uid}-subtitle-only`}
                    className="font-normal"
                  >
                    優先有字幕
                  </Label>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id={`${uid}-skip-sent`}
                    checked={opts.skip_sent}
                    onCheckedChange={(v) =>
                      setOpts({ ...opts, skip_sent: v === true })
                    }
                  />
                  <Label htmlFor={`${uid}-skip-sent`} className="font-normal">
                    跳過已送過的
                  </Label>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">檔案大小 (MB)</span>
                  <div className="flex items-center gap-1 text-xs">
                    <Input
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
                      className="h-8 w-20 px-2 text-right"
                    />
                    <span>~</span>
                    <Input
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
                      className="h-8 w-20 px-2 text-right"
                    />
                  </div>
                </div>
                <p className="text-xs text-muted-foreground">
                  範圍外或不在範圍內的磁力會跳過；大小未標示的磁力不會被過濾。
                </p>
              </div>
            )}

            {error && <ErrorBox message={error} />}

            {(busy || done) && total > 0 && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs text-muted-foreground">
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
                <ProgressBar value={percent} className="h-2" />
                <ul className="max-h-48 overflow-y-auto rounded-md border border-border bg-background/50 p-2 text-xs">
                  {recent.length === 0 && (
                    <li className="text-muted-foreground">等待第一筆…</li>
                  )}
                  {recent.map((p) => {
                    const label = STATUS_LABEL[p.status] ?? {
                      text: p.status,
                      icon: undefined,
                      cls: "text-muted-foreground",
                    };
                    const Icon = label.icon;
                    return (
                      <li
                        key={p.current}
                        className="flex items-baseline gap-2 py-0.5"
                      >
                        <span className="font-mono text-primary">{p.code}</span>
                        <span
                          className={cn(
                            "inline-flex items-center gap-1",
                            label.cls
                          )}
                        >
                          {Icon && <Icon className="h-3 w-3" aria-hidden />}
                          {label.text}
                        </span>
                        {p.magnet_name && (
                          <span className="truncate text-muted-foreground">
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
              <div className="space-y-1 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
                <div>
                  共 <strong>{result.total_movies}</strong> 部
                </div>
                <div className="inline-flex items-center gap-1 text-emerald-300">
                  <Check className="h-3.5 w-3.5" aria-hidden />
                  已送 {result.sent}
                </div>
                <div className="flex items-center gap-1 text-muted-foreground">
                  <SkipForward className="h-3.5 w-3.5" aria-hidden />
                  跳過 (無磁力 {result.skipped_no_magnet}, 已送過{" "}
                  {result.skipped_already_sent})
                </div>
                {result.failed > 0 && (
                  <div className="flex items-center gap-1 text-red-300">
                    <X className="h-3.5 w-3.5" aria-hidden />
                    失敗 {result.failed}
                  </div>
                )}
                {result.errors.length > 0 && (
                  <details className="text-xs text-muted-foreground">
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
                <Button variant="outline" onClick={cancel}>
                  取消
                </Button>
              ) : (
                <>
                  <Button variant="outline" onClick={close}>
                    關閉
                  </Button>
                  {!done && <Button onClick={submit}>開始</Button>}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
