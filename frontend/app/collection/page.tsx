"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type CollectionItem } from "@/lib/api";

const STATUS_TABS = [
  { value: "", label: "全部" },
  { value: "wishlist", label: "待看" },
  { value: "downloading", label: "下載中" },
  { value: "done", label: "完成" },
];

export default function CollectionPage() {
  const [status, setStatus] = useState("");
  const [items, setItems] = useState<CollectionItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function load(s: string) {
    setError(null);
    try {
      const q = s ? `?status=${s}` : "";
      const res = await api.get<CollectionItem[]>(`/api/collection${q}`);
      setItems(res);
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load(status);
  }, [status]);

  async function remove(code: string) {
    if (!confirm(`從收藏移除 ${code}？`)) return;
    await api.del(`/api/collection/${encodeURIComponent(code)}`);
    load(status);
  }

  async function setItemStatus(it: CollectionItem, s: string) {
    await api.post("/api/collection", { ...it, status: s });
    load(status);
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        {STATUS_TABS.map((t) => (
          <button
            key={t.value}
            onClick={() => setStatus(t.value)}
            className={
              status === t.value
                ? "btn-primary"
                : "btn-ghost"
            }
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {!items.length && (
        <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
          目前沒有收藏項目
        </div>
      )}

      <div className="grid gap-3">
        {items.map((it) => (
          <div
            key={it.code}
            className="flex gap-3 rounded-lg border border-white/10 bg-panel p-3"
          >
            {it.cover && (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={it.cover}
                alt={it.code}
                referrerPolicy="no-referrer"
                className="h-24 w-36 flex-none rounded object-cover"
              />
            )}
            <div className="flex-1">
              <Link
                href={`/movie/${encodeURIComponent(it.code)}`}
                className="font-mono text-sm font-bold text-accent hover:underline"
              >
                {it.code}
              </Link>
              <div className="text-sm text-white/80">{it.title}</div>
              <div className="mt-1 flex flex-wrap gap-1 text-xs text-white/40">
                {it.release_date && <span>{it.release_date}</span>}
                {it.duration && <span>{it.duration}</span>}
              </div>
              <div className="mt-2 flex flex-wrap gap-1">
                {it.actresses.map((a) => (
                  <span key={a} className="tag">
                    {a}
                  </span>
                ))}
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <select
                value={it.status}
                onChange={(e) => setItemStatus(it, e.target.value)}
                className="rounded bg-white/5 px-2 py-1 text-xs text-white/80"
              >
                <option value="wishlist">待看</option>
                <option value="downloading">下載中</option>
                <option value="done">完成</option>
              </select>
              <button
                onClick={() => remove(it.code)}
                className="rounded bg-red-500/20 px-2 py-1 text-xs text-red-300 hover:bg-red-500/30"
              >
                刪除
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
