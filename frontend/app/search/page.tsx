"use client";

import { FormEvent, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import MovieCard from "@/components/MovieCard";
import { MovieGridSkeleton } from "@/components/Skeleton";
import { api, type SearchResult } from "@/lib/api";

export default function SearchPage() {
  return (
    <Suspense fallback={<MovieGridSkeleton count={10} />}>
      <SearchPageInner />
    </Suspense>
  );
}

function SearchPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const initialQ = searchParams.get("q") || "";
  const initialUncensored = searchParams.get("uncensored") === "true";
  const focusOnMount = searchParams.get("focus") === "1";

  const inputRef = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState(initialQ);
  const [uncensored, setUncensored] = useState(initialUncensored);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<SearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (focusOnMount) inputRef.current?.focus();
    const handler = () => inputRef.current?.focus();
    window.addEventListener("avbt:focus-search", handler);
    return () => window.removeEventListener("avbt:focus-search", handler);
  }, [focusOnMount]);

  const run = useCallback(
    async (p: number, term: string, uc: boolean) => {
      const keyword = term.trim();
      if (!keyword) return;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({
          q: keyword,
          page: String(p),
          uncensored: String(uc),
        });
        const res = await api.get<SearchResult>(
          `/api/javbus/search?${params.toString()}`
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
    []
  );

  // Run a search whenever the URL ?q= changes (i.e. coming from dashboard).
  useEffect(() => {
    if (initialQ) run(1, initialQ, initialUncensored);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQ, initialUncensored]);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const params = new URLSearchParams({ q: q.trim(), uncensored: String(uncensored) });
    router.replace(`/search?${params.toString()}`);
    run(1, q, uncensored);
  }

  return (
    <div className="space-y-6">
      <form onSubmit={onSubmit} className="flex flex-wrap items-center gap-2">
        <input
          ref={inputRef}
          id="search-input"
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="輸入番號 / 女優 / 關鍵字"
          className="flex-1 min-w-[260px] rounded-md border border-white/10 bg-panel px-3 py-2 outline-none focus:border-accent"
        />
        <label className="flex items-center gap-2 text-sm text-white/70">
          <input
            type="checkbox"
            checked={uncensored}
            onChange={(e) => setUncensored(e.target.checked)}
          />
          無碼
        </label>
        <button type="submit" className="btn-primary" disabled={loading}>
          {loading ? "搜尋中…" : "搜尋"}
        </button>
      </form>

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
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
            {data.items.map((it) => (
              <MovieCard key={it.code + it.detail_url} item={it} />
            ))}
          </div>
          <div className="flex items-center justify-center gap-2 pt-2">
            <button
              className="btn-ghost"
              disabled={loading || page <= 1}
              onClick={() => run(page - 1, q, uncensored)}
            >
              上一頁
            </button>
            <button
              className="btn-ghost"
              disabled={loading}
              onClick={() => run(page + 1, q, uncensored)}
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
