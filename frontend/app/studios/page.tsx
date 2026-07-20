"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Building2 } from "lucide-react";
import { toast } from "@/components/Toast";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Skeleton } from "@/components/Skeleton";
import {
  api,
  imgProxy,
  type ActressBackfillStatus,
  type StudioIndexOut,
} from "@/lib/api";

type SortKey = "name" | "count";
type Scope = "tracked" | "all";

const SCOPE_STORAGE_KEY = "studios.scope";

// 已下載作品的製作商索引。資料 = PikPak 已下載番號 ∩ detail 快取,依製作商聚合。
// 與女優頁共用同一個背景建檔工作,橫幅顯示進度,邊建邊可用。
export default function StudiosPage() {
  const [data, setData] = useState<StudioIndexOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("count");
  const [scope, setScope] = useState<Scope>("tracked");
  const [toggling, setToggling] = useState(false);

  useEffect(() => {
    const saved = window.localStorage.getItem(SCOPE_STORAGE_KEY);
    if (saved === "all" || saved === "tracked") setScope(saved);
  }, []);

  function switchScope(next: Scope) {
    setScope(next);
    window.localStorage.setItem(SCOPE_STORAGE_KEY, next);
  }

  async function load() {
    setError(null);
    try {
      const res = await api.get<StudioIndexOut>("/api/studios");
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

  const trackedCount = useMemo(
    () => (data ? data.studios.filter((s) => s.tracked).length : 0),
    [data]
  );

  const list = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    const scoped =
      scope === "tracked"
        ? data.studios.filter((s) => s.tracked)
        : data.studios;
    const filtered = q
      ? scoped.filter((s) => s.name.toLowerCase().includes(q))
      : scoped;
    return [...filtered].sort((a, b) =>
      sort === "count"
        ? b.work_count - a.work_count || a.name.localeCompare(b.name, "ja")
        : a.name.localeCompare(b.name, "ja")
    );
  }, [data, query, sort, scope]);

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
        <h1 className="text-xl font-semibold">製作商</h1>
        <span className="text-sm text-muted-foreground">
          {scope === "tracked"
            ? `追蹤中 ${trackedCount} 家(全部 ${data.studios.length} 家)`
            : `${data.studios.length} 家`}
          ・已下載 {data.downloaded_total} 部
        </span>
        <div className="ml-auto flex items-center gap-2">
          {(["tracked", "all"] as Scope[]).map((k) => (
            <button
              key={k}
              onClick={() => switchScope(k)}
              className={
                "rounded-full border px-3 py-1 text-xs transition " +
                (scope === k
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:text-foreground")
              }
            >
              {k === "tracked" ? "追蹤中" : "全部"}
            </button>
          ))}
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜尋製作商…"
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
              {k === "name" ? "名稱" : "作品數"}
            </button>
          ))}
        </div>
      </div>

      {building && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-blue-400/30 bg-blue-400/10 px-3 py-2 text-sm text-blue-200">
          <span>
            製作商索引建檔中 {data.indexed_total}/{data.downloaded_total} 部
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
          icon={Building2}
          title={query ? "沒有符合的製作商" : "還沒有製作商資料"}
          hint={
            query
              ? undefined
              : "背景建檔會逐步從已下載作品的詳情整理出製作商名單,稍後再回來看看"
          }
        />
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {list.map((s) => (
            <Link
              key={s.id}
              href={`/studios/${encodeURIComponent(s.id)}`}
              className="group flex flex-col overflow-hidden rounded-lg border border-border bg-card transition hover:border-primary"
            >
              <div className="aspect-[3/2] w-full overflow-hidden bg-muted">
                {s.sample_cover ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={imgProxy(s.sample_cover)}
                    alt={s.name}
                    loading="lazy"
                    referrerPolicy="no-referrer"
                    className="h-full w-full object-cover transition group-hover:scale-105"
                  />
                ) : (
                  <div className="grid h-full w-full place-items-center text-muted-foreground/40">
                    <Building2 className="h-8 w-8" aria-hidden />
                  </div>
                )}
              </div>
              <div className="flex flex-1 flex-col gap-1 p-3">
                <div className="truncate text-sm text-foreground group-hover:text-primary">
                  {s.name}
                </div>
                <div className="text-xs text-muted-foreground">
                  {s.series_count} 系列・{s.work_count} 部
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
