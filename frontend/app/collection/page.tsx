"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import BulkSendButton from "@/components/BulkSendButton";
import { api, type CollectionItem } from "@/lib/api";

const STATUS_TABS = [
  { value: "", label: "全部" },
  { value: "wishlist", label: "待看" },
  { value: "downloading", label: "下載中" },
  { value: "done", label: "完成" },
];

const STATUS_LABELS: Record<string, string> = {
  wishlist: "待看",
  downloading: "下載中",
  done: "完成",
};

export default function CollectionPage() {
  const [status, setStatus] = useState("");
  const [items, setItems] = useState<CollectionItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

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
    setSelected(new Set());
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

  function toggle(code: string) {
    const next = new Set(selected);
    if (next.has(code)) next.delete(code);
    else next.add(code);
    setSelected(next);
  }

  function selectAll() {
    if (selected.size === items.length) setSelected(new Set());
    else setSelected(new Set(items.map((i) => i.code)));
  }

  async function batchStatus(s: string) {
    if (!selected.size) return;
    if (!confirm(`把 ${selected.size} 個項目改為「${STATUS_LABELS[s]}」？`)) return;
    const res = await api.post<{ updated: number }>(
      "/api/collection/batch/status",
      { codes: Array.from(selected), status: s }
    );
    setSelected(new Set());
    load(status);
    alert(`已更新 ${res.updated} 個`);
  }

  async function batchDelete() {
    if (!selected.size) return;
    if (!confirm(`從收藏刪除 ${selected.size} 個項目？（不會動到 PikPak）`)) return;
    const res = await api.post<{ deleted: number }>(
      "/api/collection/batch/delete",
      { codes: Array.from(selected) }
    );
    setSelected(new Set());
    load(status);
    alert(`已刪除 ${res.deleted} 個`);
  }

  const wishlistCount = items.filter((i) => i.status === "wishlist").length;
  const allSelected = selected.size > 0 && selected.size === items.length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {STATUS_TABS.map((t) => (
          <button
            key={t.value}
            onClick={() => setStatus(t.value)}
            className={status === t.value ? "btn-primary" : "btn-ghost"}
          >
            {t.label}
          </button>
        ))}
        <div className="ml-auto">
          <BulkSendButton
            streamPath="/api/collection/send-wishlist/stream"
            title="送收藏裡所有待看的"
            buttonLabel={`送全部待看的${
              wishlistCount && status === "wishlist" ? ` (${wishlistCount})` : ""
            }`}
            showMaxPages={false}
          />
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {items.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
          <label className="flex items-center gap-1 text-white/70">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={selectAll}
              className="h-4 w-4 accent-accent"
            />
            {allSelected ? "全部取消" : `全選 (${items.length})`}
          </label>
          {selected.size > 0 && (
            <>
              <span className="text-white/40">|</span>
              <span className="text-white/60">已選 {selected.size}</span>
              <div className="flex gap-1">
                <button
                  onClick={() => batchStatus("wishlist")}
                  className="rounded bg-white/10 px-2 py-1 text-xs hover:bg-white/20"
                >
                  改待看
                </button>
                <button
                  onClick={() => batchStatus("downloading")}
                  className="rounded bg-white/10 px-2 py-1 text-xs hover:bg-white/20"
                >
                  改下載中
                </button>
                <button
                  onClick={() => batchStatus("done")}
                  className="rounded bg-white/10 px-2 py-1 text-xs hover:bg-white/20"
                >
                  改完成
                </button>
                <button
                  onClick={batchDelete}
                  className="rounded bg-red-500/20 px-2 py-1 text-xs text-red-300 hover:bg-red-500/30"
                >
                  刪除
                </button>
              </div>
              <div className="ml-auto">
                <BulkSendButton
                  streamPath="/api/collection/send-by-codes/stream"
                  title={`送已選 ${selected.size} 個到 PikPak`}
                  buttonLabel={`送已選 ${selected.size} 個`}
                  showMaxPages={false}
                  extraBody={{ codes: Array.from(selected) }}
                  onDone={() => {
                    setSelected(new Set());
                    load(status);
                  }}
                />
              </div>
            </>
          )}
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
            className={
              "flex gap-3 rounded-lg border bg-panel p-3 transition " +
              (selected.has(it.code)
                ? "border-accent/60"
                : "border-white/10")
            }
          >
            <input
              type="checkbox"
              checked={selected.has(it.code)}
              onChange={() => toggle(it.code)}
              className="h-4 w-4 flex-none accent-accent self-start mt-1"
            />
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
