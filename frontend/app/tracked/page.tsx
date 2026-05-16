"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, type CheckActressResult, type TrackedActress } from "@/lib/api";

function fmt(d: string | null): string {
  if (!d) return "從未檢查";
  return new Date(d.endsWith("Z") ? d : d + "Z").toLocaleString();
}

export default function TrackedPage() {
  const [items, setItems] = useState<TrackedActress[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [checkingId, setCheckingId] = useState<string | null>(null);
  const [lastCheck, setLastCheck] = useState<CheckActressResult | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.get<TrackedActress[]>("/api/tracked");
      setItems(res);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function remove(id: string) {
    if (!confirm(`不再追蹤 ${id}？`)) return;
    await api.del(`/api/tracked/${encodeURIComponent(id)}`);
    load();
  }

  async function toggleAuto(item: TrackedActress) {
    await api.post("/api/tracked", { ...item, auto_send: !item.auto_send });
    load();
  }

  async function checkNow(id: string) {
    setCheckingId(id);
    setLastCheck(null);
    try {
      const res = await api.post<CheckActressResult>(
        `/api/tracked/${encodeURIComponent(id)}/check`
      );
      setLastCheck(res);
      load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setCheckingId(null);
    }
  }

  async function resetNew(id: string) {
    await api.post(`/api/tracked/${encodeURIComponent(id)}/reset-new-count`);
    load();
  }

  async function checkAll() {
    for (const it of items) {
      await checkNow(it.id);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">追蹤中的女優</h1>
        <span className="text-sm text-white/40">({items.length})</span>
        {items.length > 0 && (
          <button
            className="ml-auto btn-ghost"
            onClick={checkAll}
            disabled={!!checkingId}
          >
            {checkingId ? "檢查中…" : "全部立即檢查"}
          </button>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {lastCheck && (
        <div
          className={
            "rounded-md border px-3 py-2 text-sm " +
            (lastCheck.error
              ? "border-red-500/30 bg-red-500/10 text-red-300"
              : lastCheck.new_codes.length
              ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
              : "border-white/10 bg-white/5 text-white/60")
          }
        >
          {lastCheck.error
            ? `${lastCheck.name || lastCheck.id}: ${lastCheck.error}`
            : lastCheck.new_codes.length
            ? `${lastCheck.name} 有 ${lastCheck.new_codes.length} 部新作品: ${lastCheck.new_codes.join(", ")}`
            : `${lastCheck.name} 沒有新作品`}
        </div>
      )}

      {!items.length && (
        <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
          還沒追蹤任何女優。到 /star/&lt;id&gt; 點「追蹤」加入。
        </div>
      )}

      <div className="grid gap-3">
        {items.map((it) => (
          <div
            key={it.id}
            className="flex flex-wrap gap-3 rounded-lg border border-white/10 bg-panel p-3"
          >
            {it.avatar ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={it.avatar}
                alt={it.name}
                referrerPolicy="no-referrer"
                className="h-20 w-16 flex-none rounded object-cover"
              />
            ) : (
              <div className="grid h-20 w-16 flex-none place-items-center rounded bg-white/5 text-xl text-white/30">
                ?
              </div>
            )}
            <div className="min-w-0 flex-1 space-y-1">
              <div className="flex items-center gap-2">
                <Link
                  href={`/star/${encodeURIComponent(it.id)}`}
                  className="font-semibold text-accent hover:underline"
                >
                  {it.name || it.id}
                </Link>
                {it.new_count > 0 && (
                  <button
                    onClick={() => resetNew(it.id)}
                    className="rounded bg-amber-500/20 px-2 py-0.5 text-xs text-amber-300 hover:bg-amber-500/30"
                    title="點擊清除"
                  >
                    {it.new_count} 新
                  </button>
                )}
                {it.uncensored && <span className="tag">無碼</span>}
              </div>
              <div className="text-xs text-white/40">
                slug: <span className="font-mono">{it.id}</span>
                {it.last_seen_code && (
                  <>
                    {" · 最後看到: "}
                    <span className="font-mono">{it.last_seen_code}</span>
                  </>
                )}
              </div>
              <div className="text-xs text-white/40">
                最後檢查 {fmt(it.last_checked_at)}
              </div>
              {it.last_error && (
                <div className="line-clamp-2 text-xs text-amber-300/80">
                  ⚠ {it.last_error}
                </div>
              )}
            </div>
            <div className="flex flex-col items-end gap-1 text-xs">
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={it.auto_send}
                  onChange={() => toggleAuto(it)}
                />
                自動送 PikPak
              </label>
              <div className="flex gap-2">
                <button
                  onClick={() => checkNow(it.id)}
                  disabled={checkingId === it.id}
                  className="text-blue-300 hover:underline disabled:opacity-50"
                >
                  {checkingId === it.id ? "檢查中" : "立即檢查"}
                </button>
                <button
                  onClick={() => remove(it.id)}
                  className="text-red-300 hover:underline"
                >
                  取消追蹤
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
