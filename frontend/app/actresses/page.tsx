"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Users } from "lucide-react";
import { toast } from "@/components/Toast";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Skeleton } from "@/components/Skeleton";
import {
  api,
  imgProxy,
  type ActressBackfillStatus,
  type ActressIndexOut,
} from "@/lib/api";

type SortKey = "name" | "count";

// 已下載作品的女優索引。資料 = PikPak 已下載番號 ∩ detail 快取;
// 建檔工作在背景補齊缺的詳情/頭像,橫幅顯示進度,邊建邊可用。
export default function ActressesPage() {
  const [data, setData] = useState<ActressIndexOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("name");
  const [toggling, setToggling] = useState(false);

  async function load() {
    setError(null);
    try {
      const res = await api.get<ActressIndexOut>("/api/actresses");
      setData(res);
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function toggleBackfill(enabled: boolean) {
    setToggling(true);
    try {
      const res = await api.post<ActressBackfillStatus>(
        "/api/actresses/backfill/toggle",
        { enabled }
      );
      setData((prev) => (prev ? { ...prev, backfill: res } : prev));
    } catch (e: any) {
      toast.error(e.message || "切換建檔失敗");
    } finally {
      setToggling(false);
    }
  }

  // 4,000+ cards in one paint stalls the page — render a growing window
  // instead (IntersectionObserver bumps it as the sentinel scrolls in).
  const STEP = 60;
  const [visibleCount, setVisibleCount] = useState(STEP);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const list = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    const filtered = q
      ? data.actresses.filter((a) => a.name.toLowerCase().includes(q))
      : data.actresses;
    return [...filtered].sort((a, b) =>
      sort === "count"
        ? b.count - a.count || a.name.localeCompare(b.name, "ja")
        : a.name.localeCompare(b.name, "ja")
    );
  }, [data, query, sort]);

  useEffect(() => {
    setVisibleCount(STEP);
  }, [query, sort]);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const ob = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        setVisibleCount((n) => Math.min(n + STEP, list.length));
      }
    });
    ob.observe(el);
    return () => ob.disconnect();
  }, [list.length]);

  if (error) return <ErrorBox message={error} />;
  if (!data) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
        {Array.from({ length: 12 }).map((_, i) => (
          <Skeleton key={i} className="h-40 w-full" />
        ))}
      </div>
    );
  }

  const bf = data.backfill;
  const building = data.indexed_total < data.downloaded_total;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">女優</h1>
        <span className="text-sm text-muted-foreground">
          {data.actresses.length} 位・已下載 {data.downloaded_total} 部
        </span>
        <div className="ml-auto flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜尋名字…"
            className="h-9 w-40 rounded-md border border-border bg-background px-3 text-sm outline-none focus:border-primary"
          />
          {(["name", "count"] as SortKey[]).map((k) => (
            <button
              key={k}
              onClick={() => setSort(k)}
              className={
                "rounded-full border px-3 py-1 text-xs transition " +
                (sort === k
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:text-foreground")
              }
            >
              {k === "name" ? "名字" : "作品數"}
            </button>
          ))}
        </div>
      </div>

      {building && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-blue-400/30 bg-blue-400/10 px-3 py-2 text-sm text-blue-200">
          <span>
            女優索引建檔中 {data.indexed_total}/{data.downloaded_total} 部
            {bf.avatar_pending > 0 && `・頭像待補 ${bf.avatar_pending} 位`}
            {bf.last_error && `・上次錯誤:${bf.last_error}`}
          </span>
          <button
            onClick={() => toggleBackfill(!bf.enabled)}
            disabled={toggling}
            className="ml-auto rounded border border-blue-300/40 px-2 py-0.5 text-xs hover:bg-blue-400/20 disabled:opacity-40"
          >
            {bf.enabled ? "暫停建檔" : "恢復建檔"}
          </button>
        </div>
      )}

      {list.length === 0 ? (
        <EmptyState
          icon={Users}
          title={query ? "沒有符合的女優" : "還沒有女優資料"}
          hint={
            query
              ? undefined
              : "背景建檔會逐步從已下載作品的詳情整理出女優名單,稍後再回來看看"
          }
        />
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {list.slice(0, visibleCount).map((a) => (
            <Link
              key={a.name}
              href={`/actresses/${encodeURIComponent(a.name)}`}
              className="group flex flex-col items-center gap-2 rounded-lg border border-border bg-card p-4 transition hover:border-primary"
            >
              {a.avatar ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={imgProxy(a.avatar)}
                  alt={a.name}
                  loading="lazy"
                  referrerPolicy="no-referrer"
                  className="h-20 w-20 rounded-full border border-border object-cover"
                />
              ) : (
                <div className="grid h-20 w-20 place-items-center rounded-full bg-muted text-muted-foreground/50">
                  <Users className="h-8 w-8" aria-hidden />
                </div>
              )}
              <div className="w-full truncate text-center text-sm text-foreground group-hover:text-primary">
                {a.name}
              </div>
              <div className="text-xs text-muted-foreground">{a.count} 部</div>
            </Link>
          ))}
        </div>
      )}
      {visibleCount < list.length && (
        <div ref={sentinelRef} className="py-4 text-center text-sm text-muted-foreground">
          載入中…({visibleCount} / {list.length})
        </div>
      )}
    </div>
  );
}
