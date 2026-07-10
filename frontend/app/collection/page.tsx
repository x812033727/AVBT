"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import BulkSendButton from "@/components/BulkSendButton";
import { RowSkeleton } from "@/components/Skeleton";
import { confirmDialog, toast } from "@/components/Toast";
import {
  api,
  imgProxy,
  type CollectionItem,
  type VideoCountResponse,
  type VideoCountResult,
} from "@/lib/api";

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
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // 影片數快取(key = code)。"loading" = 查詢中。
  const [counts, setCounts] = useState<Record<string, VideoCountResult | "loading">>({});
  const [counting, setCounting] = useState(false);
  const [syncing, setSyncing] = useState(false);

  async function syncStatus() {
    setSyncing(true);
    try {
      const r = await api.post<{
        checked: number;
        to_downloading: number;
        to_done: number;
      }>("/api/collection/sync-status");
      const changed = r.to_downloading + r.to_done;
      toast.success(
        changed
          ? `已同步:${r.to_done} 個標為完成、${r.to_downloading} 個標為下載中`
          : `已檢查 ${r.checked} 個收藏,狀態都是最新的`
      );
      if (changed) load(status);
    } catch (e: any) {
      toast.error(`同步失敗:${e.message}`);
    } finally {
      setSyncing(false);
    }
  }

  async function load(s: string) {
    setError(null);
    setLoading(true);
    try {
      const q = s ? `?status=${s}` : "";
      const res = await api.get<CollectionItem[]>(`/api/collection${q}`);
      setItems(res);
    } catch (e: any) {
      setError(e.message);
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(status);
    setSelected(new Set());
  }, [status]);

  async function remove(code: string) {
    const ok = await confirmDialog(`從收藏移除 ${code}？`);
    if (!ok) return;
    try {
      await api.del(`/api/collection/${encodeURIComponent(code)}`);
      toast.success(`已移除 ${code}`);
      load(status);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function setItemStatus(it: CollectionItem, s: string) {
    try {
      await api.post("/api/collection", { ...it, status: s });
      load(status);
    } catch (e: any) {
      toast.error(e.message);
    }
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
    const ok = await confirmDialog(
      `把 ${selected.size} 個項目改為「${STATUS_LABELS[s]}」？`
    );
    if (!ok) return;
    try {
      const res = await api.post<{ updated: number }>(
        "/api/collection/batch/status",
        { codes: Array.from(selected), status: s }
      );
      setSelected(new Set());
      load(status);
      toast.success(`已更新 ${res.updated} 個`);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function batchDelete() {
    if (!selected.size) return;
    const ok = await confirmDialog(
      `從收藏刪除 ${selected.size} 個項目？`,
      "不會動到 PikPak 上的檔案"
    );
    if (!ok) return;
    try {
      const res = await api.post<{ deleted: number }>(
        "/api/collection/batch/delete",
        { codes: Array.from(selected) }
      );
      setSelected(new Set());
      load(status);
      toast.success(`已刪除 ${res.deleted} 個`);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // 待看通常還沒下載;下載中/完成才可能有雲端檔案。
  const countable = items.filter(
    (i) => i.status !== "wishlist" && !counts[i.code]
  );

  async function fetchCounts() {
    if (!countable.length) return;
    setCounting(true);
    setCounts((prev) => {
      const next = { ...prev };
      for (const it of countable) next[it.code] = "loading";
      return next;
    });
    try {
      for (let i = 0; i < countable.length; i += 20) {
        const chunk = countable.slice(i, i + 20);
        const res = await api.post<VideoCountResponse>(
          "/api/pikpak/files/video-count",
          { items: chunk.map((it) => ({ key: it.code, code: it.code })) }
        );
        setCounts((prev) => {
          const next = { ...prev };
          for (const r of res.results) next[r.key] = r;
          return next;
        });
      }
    } catch (e: any) {
      toast.error(`影片數查詢失敗:${e.message}`);
      setCounts((prev) => {
        const next = { ...prev };
        for (const it of countable) {
          if (next[it.code] === "loading") delete next[it.code];
        }
        return next;
      });
    } finally {
      setCounting(false);
    }
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
        <button
          onClick={fetchCounts}
          disabled={counting || !countable.length}
          className="btn-ghost disabled:opacity-50"
          title="向 PikPak 查詢下載中/完成項目的實際影片檔數(分集/單一)"
        >
          {counting ? "查詢中…" : "查詢影片數"}
        </button>
        <button
          onClick={syncStatus}
          disabled={syncing}
          className="btn-ghost disabled:opacity-50"
          title="依雲端實況調整狀態:已送 PikPak → 下載中;檔案已在雲端/已歸檔 → 完成。只往前推進,不會降級"
        >
          {syncing ? "同步中…" : "依雲端狀態同步"}
        </button>
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

      {loading && !items.length && <RowSkeleton count={5} />}

      {!loading && !items.length && (
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
                src={imgProxy(it.cover)}
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
                <CountBadge state={counts[it.code]} />
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

function CountBadge({
  state,
}: {
  state: VideoCountResult | "loading" | undefined;
}) {
  if (state === undefined) return null;
  if (state === "loading") return <span className="text-white/30">…</span>;
  if (!state.ok) return null;
  const tip = state.video_names.join("\n") || undefined;
  if (state.video_count > 1) {
    return (
      <span
        className="rounded bg-amber-400/20 px-1.5 py-0.5 text-amber-200"
        title={tip}
      >
        多集 {state.video_count}
      </span>
    );
  }
  if (state.video_count === 1) {
    return (
      <span className="text-white/50" title={tip}>
        單一影片
      </span>
    );
  }
  return null;
}
