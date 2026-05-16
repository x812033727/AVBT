"use client";

import { useCallback, useEffect, useState } from "react";
import MovieCard from "@/components/MovieCard";
import { api, type SearchResult } from "@/lib/api";

export default function GenrePage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);
  const [uncensored, setUncensored] = useState(false);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<SearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
          `/api/javbus/genre/${encodeURIComponent(id)}?${params.toString()}`
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
    [id, uncensored]
  );

  useEffect(() => {
    run(1);
  }, [run]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div>
          <div className="text-xs text-white/40">類別</div>
          <h1 className="font-mono text-lg text-accent">{id}</h1>
        </div>
        <label className="ml-auto flex items-center gap-2 text-sm text-white/70">
          <input
            type="checkbox"
            checked={uncensored}
            onChange={(e) => setUncensored(e.target.checked)}
          />
          無碼
        </label>
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
