"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import BulkSendButton from "@/components/BulkSendButton";
import MovieCard from "@/components/MovieCard";
import { toast } from "@/components/Toast";
import {
  TRACKED_LABELS,
  api,
  type AggregatedMissing,
  type MovieListItem,
  type TrackedKind,
} from "@/lib/api";

const KIND_COLORS: Record<TrackedKind, string> = {
  star: "bg-pink-500/20 text-pink-300",
  studio: "bg-blue-500/20 text-blue-300",
  label: "bg-violet-500/20 text-violet-300",
  series: "bg-emerald-500/20 text-emerald-300",
  director: "bg-amber-500/20 text-amber-300",
  genre: "bg-cyan-500/20 text-cyan-300",
};

function fmt(d: string | null): string {
  if (!d) return "從未建立";
  return new Date(d.endsWith("Z") ? d : d + "Z").toLocaleString();
}

export default function MissingPage() {
  const [data, setData] = useState<AggregatedMissing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<TrackedKind | "">("");
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get<AggregatedMissing>(
        `/api/tracked/missing-all${refresh ? "?refresh=true" : ""}`
      );
      setData(res);
      setSelected(new Set());
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(false);
  }, [load]);

  const sections = useMemo(
    () => data?.items?.filter((s) => !filter || s.kind === filter) || [],
    [data, filter]
  );
  const totalMissing = sections.reduce((n, s) => n + s.missing.length, 0);

  // The same code can be missing under several listings — dedupe for
  // the action payloads.
  const selectedItems = useMemo(() => {
    const byCode = new Map<string, MovieListItem>();
    for (const s of sections) {
      for (const it of s.missing) {
        if (selected.has(it.code) && !byCode.has(it.code)) {
          byCode.set(it.code, it);
        }
      }
    }
    return Array.from(byCode.values());
  }, [sections, selected]);

  function toggleCode(code: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(code);
      else next.delete(code);
      return next;
    });
  }

  function selectSection(codes: string[], on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const c of codes) {
        if (on) next.add(c);
        else next.delete(c);
      }
      return next;
    });
  }

  async function addToCollection() {
    if (!selectedItems.length) return;
    setBusy(true);
    try {
      const r = await api.post<{ added: number; skipped: number }>(
        "/api/collection/batch/add",
        {
          items: selectedItems.map((it) => ({
            code: it.code,
            title: it.title,
            cover: it.cover,
            release_date: it.date,
            status: "wishlist",
          })),
        }
      );
      toast.success(`已加入收藏 ${r.added} 部(略過已存在 ${r.skipped})`);
      setSelected(new Set());
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">缺漏番號</h1>
        <span className="text-sm text-white/40">
          {sections.length} 個分類 ・ 共 {totalMissing} 部缺漏
        </span>
        <div className="flex flex-wrap gap-1">
          {([
            { v: "", l: "全部" },
            { v: "star", l: "女優" },
            { v: "studio", l: "製作商" },
            { v: "label", l: "發行商" },
            { v: "series", l: "系列" },
            { v: "director", l: "導演" },
            { v: "genre", l: "類別" },
          ] as { v: TrackedKind | ""; l: string }[]).map((f) => (
            <button
              key={f.v}
              onClick={() => setFilter(f.v)}
              className={
                "rounded-md px-3 py-1 text-xs " +
                (filter === f.v
                  ? "bg-accent text-black"
                  : "border border-white/10 text-white/60 hover:bg-white/5")
              }
            >
              {f.l}
            </button>
          ))}
        </div>
        <button
          className={
            "rounded-md px-3 py-1 text-xs " +
            (selectMode
              ? "bg-accent text-black"
              : "border border-white/10 text-white/60 hover:bg-white/5")
          }
          onClick={() => {
            setSelectMode((m) => !m);
            setSelected(new Set());
          }}
        >
          {selectMode ? "結束選取" : "選取模式"}
        </button>
        <button
          className="ml-auto btn-ghost"
          onClick={() => load(true)}
          disabled={loading}
        >
          {loading ? "重算中…" : "重新整理"}
        </button>
      </div>

      <div className="text-xs text-white/40">
        建立於 {fmt(data?.built_at || null)} ・ PikPak 索引{" "}
        {fmt(data?.presence_built_at || null)}
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {!loading && sections.length === 0 && (
        <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
          {data
            ? "沒有缺漏 — 所有追蹤分類都已收齊。"
            : "載入中…"}
        </div>
      )}

      {sections.map((s) => {
        const codes = s.missing.map((it) => it.code);
        const allSelected =
          codes.length > 0 && codes.every((c) => selected.has(c));
        return (
          <section
            key={`${s.kind}:${s.id}`}
            className="space-y-3 rounded-lg border border-white/10 bg-panel/40 p-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className={"rounded px-2 py-0.5 text-xs " + KIND_COLORS[s.kind]}>
                {TRACKED_LABELS[s.kind]}
              </span>
              <Link
                href={`/${s.kind}/${encodeURIComponent(s.id)}`}
                className="font-semibold text-accent hover:underline"
              >
                {s.name || s.id}
              </Link>
              <span className="text-xs text-white/40">
                缺 {s.missing.length} 部
              </span>
              {selectMode && (
                <button
                  className="rounded border border-white/10 px-2 py-0.5 text-xs text-white/60 hover:bg-white/10"
                  onClick={() => selectSection(codes, !allSelected)}
                >
                  {allSelected ? "取消全選" : "全選此分類"}
                </button>
              )}
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
              {s.missing.map((it) => (
                <MovieCard
                  key={it.code + it.detail_url}
                  item={it}
                  present={false}
                  selectable={selectMode}
                  selected={selected.has(it.code)}
                  onToggleSelect={toggleCode}
                />
              ))}
            </div>
          </section>
        );
      })}

      {selectMode && selected.size > 0 && (
        <div className="sticky bottom-3 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-white/10 bg-panel/95 px-4 py-3 shadow-lg backdrop-blur">
          <span className="text-sm text-white/70">
            已選 {selectedItems.length} 部
          </span>
          <BulkSendButton
            streamPath="/api/collection/send-by-codes/stream"
            title={`送出已選的 ${selectedItems.length} 部到 PikPak`}
            buttonLabel={`送出下載 (${selectedItems.length})`}
            showMaxPages={false}
            extraBody={{ codes: selectedItems.map((it) => it.code) }}
            onDone={() => setSelected(new Set())}
            disabled={busy}
          />
          <button
            className="rounded-md border border-white/10 px-3 py-1.5 text-sm text-white/80 transition hover:bg-white/5 disabled:opacity-50"
            onClick={addToCollection}
            disabled={busy}
          >
            加入收藏(待看)
          </button>
          <button
            className="btn-ghost text-sm"
            onClick={() => setSelected(new Set())}
            disabled={busy}
          >
            清除選取
          </button>
        </div>
      )}
    </div>
  );
}
