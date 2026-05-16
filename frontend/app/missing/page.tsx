"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import MovieCard from "@/components/MovieCard";
import {
  TRACKED_LABELS,
  api,
  type AggregatedMissing,
  type TrackedKind,
} from "@/lib/api";

const KIND_COLORS: Record<TrackedKind, string> = {
  star: "bg-pink-500/20 text-pink-300",
  studio: "bg-blue-500/20 text-blue-300",
  label: "bg-violet-500/20 text-violet-300",
  series: "bg-emerald-500/20 text-emerald-300",
  director: "bg-amber-500/20 text-amber-300",
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

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get<AggregatedMissing>(
        `/api/tracked/missing-all${refresh ? "?refresh=true" : ""}`
      );
      setData(res);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(false);
  }, [load]);

  const sections = data?.items?.filter((s) => !filter || s.kind === filter) || [];
  const totalMissing = sections.reduce((n, s) => n + s.missing.length, 0);

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

      {sections.map((s) => (
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
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
            {s.missing.map((it) => (
              <MovieCard
                key={it.code + it.detail_url}
                item={it}
                present={false}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
