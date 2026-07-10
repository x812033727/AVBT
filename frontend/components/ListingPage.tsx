"use client";

import { useCallback, useEffect, useState } from "react";
import BulkSendButton from "@/components/BulkSendButton";
import MovieCard from "@/components/MovieCard";
import { MovieGridSkeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
import {
  api,
  type MissingCodesResult,
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
  const [presence, setPresence] = useState<Set<string> | null>(null);
  const [presenceMeta, setPresenceMeta] = useState<{
    total: number;
    missing: number;
    extras: number;
    expected_root: string;
  } | null>(null);
  const [presenceBusy, setPresenceBusy] = useState(false);
  const [presenceError, setPresenceError] = useState<string | null>(null);
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
        if (res.items.length === 0 && p > 1) {
          setError(`已是最後一頁（第 ${p} 頁無內容）`);
          return;
        }
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

  const loadPresence = useCallback(
    async (refresh: boolean) => {
      if (!trackable || !tracked) return;
      setPresenceBusy(true);
      setPresenceError(null);
      try {
        const params = new URLSearchParams({
          uncensored: String(uncensored),
        });
        if (refresh) params.set("refresh", "true");
        const r = await api.get<MissingCodesResult>(
          `/api/tracked/${kind}/${encodeURIComponent(id)}/missing-codes?${params}`
        );
        setPresence(new Set(r.present_codes));
        setPresenceMeta({
          total: r.total,
          missing: r.missing.length,
          extras: r.extras.length,
          expected_root: r.expected_root,
        });
      } catch (e: any) {
        // Don't swallow: a JavBus 429/5xx (or any /missing-codes failure)
        // used to make the whole presence card vanish with no explanation.
        // Surface it so the user knows the count is unavailable, not zero.
        const msg = e?.message || "讀取缺漏失敗";
        setPresence(null);
        setPresenceMeta(null);
        setPresenceError(msg);
        if (refresh) toast.error(`缺漏重算失敗：${msg}`);
      } finally {
        setPresenceBusy(false);
      }
    },
    [kind, id, uncensored, trackable, tracked]
  );

  useEffect(() => {
    loadPresence(false);
  }, [loadPresence]);

  async function toggleTrack() {
    if (tracked) {
      await api.del(`/api/tracked/${kind}/${encodeURIComponent(id)}`);
      setTracked(null);
    } else {
      // Send name="" so the backend pulls the real display name from the
      // listing page header (e.g. "SODクリエイト" instead of slug "ca").
      const t = await api.post<TrackedListing>("/api/tracked", {
        kind: kind as TrackedKind,
        id,
        name: "",
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
          {tracked?.name ? (
            <>
              <h1 className="text-lg font-semibold text-white/90">
                {tracked.name}
              </h1>
              <div className="font-mono text-xs text-white/40">
                slug: {id}
              </div>
            </>
          ) : (
            <h1 className="font-mono text-lg text-accent">{id}</h1>
          )}
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

      {loading && !data && <MovieGridSkeleton count={10} />}

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
          {tracked && presenceMeta && (
            <div className="space-y-1 rounded-md border border-white/10 bg-panel/40 px-3 py-2 text-xs text-white/70">
              <div className="flex flex-wrap items-center gap-3">
                <span>
                  追蹤全集共{" "}
                  <span className="font-semibold text-white/90">
                    {presenceMeta.total}
                  </span>{" "}
                  部 ・{" "}
                  <span className="text-emerald-300">
                    已下載 {presenceMeta.total - presenceMeta.missing}
                  </span>{" "}
                  ／{" "}
                  <span className="text-amber-300">
                    缺漏 {presenceMeta.missing}
                  </span>
                  {presenceMeta.extras > 0 && (
                    <>
                      {" "}・{" "}
                      <span
                        className="text-purple-300"
                        title="此資料夾有,但不在 JavBus 列表內的番號"
                      >
                        多餘 {presenceMeta.extras}
                      </span>
                    </>
                  )}
                </span>
                <button
                  onClick={() => loadPresence(true)}
                  disabled={presenceBusy}
                  className="ml-auto rounded border border-white/15 px-2 py-0.5 hover:bg-white/5 disabled:opacity-40"
                >
                  {presenceBusy ? "重建中…" : "重新整理 PikPak 索引"}
                </button>
              </div>
              {presenceMeta.expected_root && (
                <div className="text-white/40">
                  判斷路徑:
                  <span className="ml-1 font-mono text-white/70">
                    {presenceMeta.expected_root}/&lt;番號&gt;
                  </span>
                </div>
              )}
            </div>
          )}
          {tracked && presenceError && !presenceMeta && (
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              <span>
                缺漏讀取失敗:{presenceError}
                <span className="ml-1 text-red-300/70">
                  (JavBus 可能限流 / 暫時無法連線,稍後再試)
                </span>
              </span>
              <button
                onClick={() => loadPresence(true)}
                disabled={presenceBusy}
                className="ml-auto rounded border border-red-400/30 px-2 py-0.5 hover:bg-red-500/15 disabled:opacity-40"
              >
                {presenceBusy ? "重試中…" : "重試"}
              </button>
            </div>
          )}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
            {data.items.map((it) => (
              <MovieCard
                key={it.code + it.detail_url}
                item={it}
                present={presence ? presence.has(it.code) : undefined}
              />
            ))}
          </div>
          <div className="flex items-center justify-center gap-2 pt-2">
            <button
              className="btn-ghost"
              disabled={loading || page <= 1}
              onClick={() => run(page - 1)}
            >
              上一頁
            </button>
            <button
              className="btn-ghost"
              disabled={loading}
              onClick={() => run(page + 1)}
              title={!data.has_next ? "後端沒偵測到下一頁，但仍可嘗試" : undefined}
            >
              下一頁
            </button>
            {!data.has_next && (
              <span className="text-xs text-white/40">（已到底）</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
