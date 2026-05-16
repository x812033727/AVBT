"use client";

import { useState } from "react";
import { api } from "@/lib/api";

type Options = {
  uncensored: boolean;
  max_pages: number;
  hd_only: boolean;
  subtitle_only: boolean;
  skip_sent: boolean;
};

type Result = {
  total_movies: number;
  sent: number;
  skipped_no_magnet: number;
  skipped_already_sent: number;
  failed: number;
  errors: string[];
};

export default function BulkSendButton({
  kind,
  slug,
  uncensored,
}: {
  kind: "star" | "genre";
  slug: string;
  uncensored: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opts, setOpts] = useState<Options>({
    uncensored,
    max_pages: 5,
    hd_only: true,
    subtitle_only: false,
    skip_sent: true,
  });

  async function submit() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.post<Result>(
        `/api/javbus/${kind}/${encodeURIComponent(slug)}/send-all`,
        { ...opts, uncensored }
      );
      setResult(res);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button className="btn-primary" onClick={() => setOpen(true)}>
        送全部到 PikPak
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
          onClick={(e) => {
            if (e.target === e.currentTarget && !busy) setOpen(false);
          }}
        >
          <div className="w-full max-w-md space-y-4 rounded-xl border border-white/10 bg-panel p-5">
            <div className="flex items-center">
              <h2 className="text-lg font-semibold">
                送 {kind === "star" ? "女優" : "類別"}「{slug}」全部
              </h2>
              <button
                className="ml-auto text-white/40 hover:text-white"
                onClick={() => !busy && setOpen(false)}
              >
                ✕
              </button>
            </div>

            <div className="space-y-3 text-sm">
              <label className="flex items-center justify-between">
                <span className="text-white/70">最多抓幾頁</span>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={opts.max_pages}
                  onChange={(e) =>
                    setOpts({ ...opts, max_pages: parseInt(e.target.value || "1") })
                  }
                  className="w-20 rounded-md border border-white/10 bg-ink px-2 py-1 text-right"
                />
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={opts.hd_only}
                  onChange={(e) => setOpts({ ...opts, hd_only: e.target.checked })}
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
                  onChange={(e) => setOpts({ ...opts, skip_sent: e.target.checked })}
                />
                <span>跳過已送過的</span>
              </label>
            </div>

            {error && (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            )}

            {result && (
              <div className="space-y-1 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
                <div>
                  共 <strong>{result.total_movies}</strong> 部作品
                </div>
                <div className="text-emerald-300">
                  ✓ 已送 {result.sent}
                </div>
                <div className="text-white/60">
                  ⏭ 跳過 (無磁力 {result.skipped_no_magnet}, 已送過{" "}
                  {result.skipped_already_sent})
                </div>
                {result.failed > 0 && (
                  <div className="text-red-300">✗ 失敗 {result.failed}</div>
                )}
                {result.errors.length > 0 && (
                  <details className="text-xs text-white/50">
                    <summary>錯誤明細</summary>
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
              <button
                className="btn-ghost"
                onClick={() => setOpen(false)}
                disabled={busy}
              >
                關閉
              </button>
              <button
                className="btn-primary disabled:opacity-50"
                onClick={submit}
                disabled={busy}
              >
                {busy ? "處理中…（可能要一兩分鐘）" : "開始"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
