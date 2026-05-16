"use client";

import { useCallback, useEffect, useState } from "react";
import BulkSendButton from "@/components/BulkSendButton";
import MovieCard from "@/components/MovieCard";
import {
  api,
  type SearchResult,
  type TrackedKind,
  type TrackedListing,
} from "@/lib/api";

export default function ListingPage({
  kind,
  id,
  label,
}: {
  /** JavBus URL kind */
  kind: "studio" | "label" | "series" | "director" | "genre";
  /** JavBus slug (the bit after /{kind}/) */
  id: string;
  /** Human-readable label, e.g. "製作商" */
  label: string;
}) {
  const [uncensored, setUncensored] = useState(false);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<SearchResult | null>(null);
  const [tracked, setTracked] = useState<TrackedListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trackable = kind !== "genre";  // 類別變動太頻繁，不適合做追蹤

  const run = useCallback(
    async (p: number) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({
          page: String(p),
          uncensored: String(uncensored),
        });
        const res = await api.get<SearchResult>(
          `/api/javbus/${kind}/${encodeURIComponent(id)}?${params.toString()}`
        );
        setData(res);
        setPage(p);
      } catch (e: any) {
        setError(e.message);
        setData(null);
      } finally {
        setLoading(false);
      }
    },
    [kind, id, uncensored]
  );

  useEffect(() => {
    run(1);
  }, [run]);

  useEffect(() => {
    if (!trackable) return;
    let alive = true;
    api
      .get<TrackedListing>(`/api/tracked/${kind}/${encodeURIComponent(id)}`)
      .then((t) => alive && setTracked(t))
      .catch(() => alive && setTracked(null));
    return () => {
      alive = false;
    };
  }, [kind, id, trackable]);

  async function toggleTrack() {
    if (tracked) {
      await api.del(`/api/tracked/${kind}/${encodeURIComponent(id)}`);
      setTracked(null);
    } else {
      const t = await api.post<TrackedListing>("/api/tracked", {
        kind: kind as TrackedKind,
        id,
        name: id,  // 列表頁不抓 profile，先用 slug，使用者可在 /tracked 頁編輯
        avatar: "",
        uncensored,
        auto_send: false,
      });
      setTracked(t);
    }
  }

  async function toggleAutoSend() {
    if (!tracked) return;
    const t = await api.post<TrackedListing>("/api/tracked", {
      ...tracked,
      auto_send: !tracked.auto_send,
    });
    setTracked(t);
  }

  const firstTitle = data?.items?.[0]?.title || "";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <div className="text-xs text-white/40">{label}</div>
          <h1 className="font-mono text-lg text-accent">{id}</h1>
        </div>
        <label className="flex items-center gap-2 text-sm text-white/70">
          <input
            type="checkbox"
            checked={uncensored}
            onChange={(e) => setUncensored(e.target.checked)}
          />
          無碼
        </label>
        <div className="ml-auto flex flex-col items-end gap-2">
          <div className="flex gap-2">
            {trackable && (
              <button
                onClick={toggleTrack}
                className={tracked ? "btn-ghost" : "btn-primary"}
              >
                {tracked ? "✓ 已追蹤" : "★ 追蹤"}
              </button>
            )}
            <BulkSendButton
              streamPath={`/api/javbus/${kind}/${encodeURIComponent(id)}/send-all/stream`}
              title={`送${label}「${id}」全部`}
              defaultOptions={{ uncensored }}
            />
          </div>
          {tracked && (
            <label className="flex items-center gap-1 text-xs text-white/60">
              <input
                type="checkbox"
                checked={tracked.auto_send}
                onChange={toggleAutoSend}
              />
              新作品自動送 PikPak
            </label>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {loading && <div className="text-sm text-white/50">載入中…</div>}

      {data && (
        <>
          <div className="text-sm text-white/50">
            第 {data.page} 頁
            {data.total_pages ? ` / 共 ${data.total_pages} 頁` : ""}，共{" "}
            {data.items.length} 筆
            {firstTitle && (
              <span className="ml-2 text-white/30">・最新：{firstTitle}</span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {data.items.map((it) => (
              <MovieCard key={it.code + it.detail_url} item={it} />
            ))}
          </div>
          <div className="flex justify-center gap-2 pt-2">
            <button
              className="btn-ghost"
              disabled={loading || page <= 1}
              onClick={() => run(page - 1)}
            >
              上一頁
            </button>
            <button
              className="btn-ghost"
              disabled={loading || !data.has_next}
              onClick={() => run(page + 1)}
            >
              下一頁
            </button>
          </div>
        </>
      )}
    </div>
  );
}
