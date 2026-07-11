"use client";

import { FormEvent, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { SearchX } from "lucide-react";
import MovieCard from "@/components/MovieCard";
import { MovieGridSkeleton } from "@/components/Skeleton";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { MovieGrid } from "@/components/shared/MovieGrid";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, type SearchResult } from "@/lib/api";

export default function SearchPage() {
  return (
    <Suspense fallback={<MovieGridSkeleton count={10} />}>
      <SearchPageInner />
    </Suspense>
  );
}

const HISTORY_KEY = "avbt:search-history";
const HISTORY_MAX = 12;

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter((s) => typeof s === "string") : [];
  } catch {
    return [];
  }
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
  const [history, setHistory] = useState<string[]>([]);

  useEffect(() => {
    setHistory(loadHistory());
  }, []);

  const remember = useCallback((term: string) => {
    const keyword = term.trim();
    if (!keyword) return;
    setHistory((prev) => {
      const next = [keyword, ...prev.filter((s) => s !== keyword)].slice(
        0,
        HISTORY_MAX
      );
      try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
      } catch {
        /* storage full/blocked — history is best-effort */
      }
      return next;
    });
  }, []);

  function clearHistory() {
    setHistory([]);
    try {
      localStorage.removeItem(HISTORY_KEY);
    } catch {
      /* ignore */
    }
  }
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
    if (initialQ) {
      remember(initialQ);
      run(1, initialQ, initialUncensored);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQ, initialUncensored]);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const params = new URLSearchParams({ q: q.trim(), uncensored: String(uncensored) });
    router.replace(`/search?${params.toString()}`);
    remember(q);
    run(1, q, uncensored);
  }

  function searchFromHistory(term: string) {
    setQ(term);
    const params = new URLSearchParams({ q: term, uncensored: String(uncensored) });
    router.replace(`/search?${params.toString()}`);
    remember(term);
    run(1, term, uncensored);
  }

  return (
    <div className="space-y-6">
      <form onSubmit={onSubmit} className="flex flex-wrap items-center gap-2">
        <Input
          ref={inputRef}
          id="search-input"
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="輸入番號 / 女優 / 關鍵字"
          list="search-history"
          className="min-w-[260px] flex-1"
        />
        <datalist id="search-history">
          {history.map((h) => (
            <option key={h} value={h} />
          ))}
        </datalist>
        <div className="flex items-center gap-2">
          <Checkbox
            id="search-uncensored"
            checked={uncensored}
            onCheckedChange={(v) => setUncensored(v === true)}
          />
          <Label
            htmlFor="search-uncensored"
            className="text-sm font-normal text-muted-foreground"
          >
            無碼
          </Label>
        </div>
        <Button type="submit" disabled={loading}>
          {loading ? "搜尋中…" : "搜尋"}
        </Button>
      </form>

      {history.length > 0 && !data && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">最近搜尋:</span>
          {history.map((h) => (
            <button
              key={h}
              type="button"
              onClick={() => searchFromHistory(h)}
              className="rounded-full border border-border px-3 py-1 text-muted-foreground transition hover:border-primary hover:text-primary"
            >
              {h}
            </button>
          ))}
          <button
            type="button"
            onClick={clearHistory}
            className="text-muted-foreground/60 transition hover:text-foreground"
            title="清除搜尋紀錄"
          >
            清除
          </button>
        </div>
      )}

      {error && <ErrorBox message={error} />}

      {loading && !data && <MovieGridSkeleton count={10} />}

      {data && (
        <>
          <div className="text-sm text-muted-foreground">
            第 {data.page} 頁
            {data.total_pages ? ` / 共 ${data.total_pages} 頁` : ""}，共{" "}
            {data.items.length} 筆
          </div>
          {data.items.length === 0 ? (
            <EmptyState
              icon={SearchX}
              title="沒有符合的結果"
              hint="換個番號或關鍵字再試一次"
            />
          ) : (
            <MovieGrid>
              {data.items.map((it) => (
                <MovieCard key={it.code + it.detail_url} item={it} />
              ))}
            </MovieGrid>
          )}
          <div className="flex items-center justify-center gap-2 pt-2">
            <Button
              variant="outline"
              disabled={loading || page <= 1}
              onClick={() => run(page - 1, q, uncensored)}
            >
              上一頁
            </Button>
            <Button
              variant="outline"
              disabled={loading}
              onClick={() => run(page + 1, q, uncensored)}
              title={!data.has_next ? "後端沒偵測到下一頁，但仍可嘗試" : undefined}
            >
              下一頁
            </Button>
            {!data.has_next && (
              <span className="text-xs text-muted-foreground">（已到底）</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
