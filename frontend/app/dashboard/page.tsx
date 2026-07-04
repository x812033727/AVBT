"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import TrendBars from "@/components/TrendBars";
import {
  api,
  TRACKED_LABELS,
  type DashboardStats,
  type PCloudStatus,
  type PikPakStatus,
} from "@/lib/api";

function fmtBytes(n?: number | null) {
  if (!n) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${u[i]}`;
}

const STATUS_LABELS: Record<string, string> = {
  wishlist: "待看",
  downloading: "下載中",
  done: "已完成",
};

const PHASE_LABELS: Record<string, string> = {
  PHASE_TYPE_COMPLETE: "完成",
  PHASE_TYPE_RUNNING: "進行中",
  PHASE_TYPE_PENDING: "等待中",
  PHASE_TYPE_ERROR: "失敗",
};

const TRANSFER_LABELS: Record<string, string> = {
  pending: "等待中",
  running: "進行中",
  done: "完成",
  failed: "失敗",
  cancelled: "已取消",
};

function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-white/10 bg-panel p-4">
      <div className="text-sm text-white/50">{label}</div>
      <div className="mt-1 text-2xl font-bold">{value}</div>
      {sub ? <div className="mt-0.5 text-xs text-white/40">{sub}</div> : null}
    </div>
  );
}

function QuotaBar({
  label,
  used,
  limit,
  error,
}: {
  label: string;
  used?: number;
  limit?: number;
  error?: string;
}) {
  const pct = used && limit ? Math.min(100, (used / limit) * 100) : 0;
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-white/70">{label}</span>
        <span className="text-xs text-white/40">
          {error
            ? `無法取得:${error}`
            : limit
              ? `${fmtBytes(used)} / ${fmtBytes(limit)}(${pct.toFixed(1)}%)`
              : "未登入"}
        </span>
      </div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-white/10">
        <div
          className={`h-full rounded-full ${pct > 90 ? "bg-red-500" : "bg-accent"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function CountChips({
  counts,
  labels,
}: {
  counts: Record<string, number>;
  labels: Record<string, string>;
}) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return <div className="text-sm text-white/40">無資料</div>;
  return (
    <div className="flex flex-wrap gap-2">
      {entries.map(([k, v]) => (
        <span
          key={k}
          className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-sm"
        >
          {labels[k] ?? k}
          <span className="ml-1.5 font-mono text-white/60">{v}</span>
        </span>
      ))}
    </div>
  );
}

function TopList({ title, items }: { title: string; items: { name: string; count: number }[] }) {
  const max = Math.max(1, ...items.map((i) => i.count));
  return (
    <div className="rounded-xl border border-white/10 bg-panel p-4">
      <h2 className="mb-3 text-sm font-semibold text-white/70">{title}</h2>
      {items.length === 0 ? (
        <div className="text-sm text-white/40">收藏裡還沒有資料</div>
      ) : (
        <ul className="space-y-2">
          {items.map((it) => (
            <li key={it.name} className="text-sm">
              <div className="flex items-baseline justify-between">
                <span className="truncate">{it.name}</span>
                <span className="ml-2 font-mono text-white/50">{it.count}</span>
              </div>
              <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-white/5">
                <div
                  className="h-full rounded-full bg-accent/60"
                  style={{ width: `${(it.count / max) * 100}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [pikpak, setPikpak] = useState<PikPakStatus | null>(null);
  const [pcloud, setPcloud] = useState<PCloudStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    // Quotas load independently — a slow cloud API must not block the
    // DB-side numbers.
    const [s, pk, pc] = await Promise.allSettled([
      api.get<DashboardStats>("/api/stats/dashboard"),
      api.get<PikPakStatus>("/api/pikpak/status"),
      api.get<PCloudStatus>("/api/pcloud/status"),
    ]);
    if (s.status === "fulfilled") setStats(s.value);
    else setError((s.reason as Error).message);
    if (pk.status === "fulfilled") setPikpak(pk.value);
    if (pc.status === "fulfilled") setPcloud(pc.value);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <main className="mx-auto max-w-6xl space-y-6 px-4 py-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">統計總覽</h1>
        <button
          onClick={load}
          disabled={loading}
          className="rounded-md border border-white/10 px-3 py-1.5 text-sm text-white/70 transition hover:bg-white/5 disabled:opacity-50"
        >
          {loading ? "載入中…" : "重新整理"}
        </button>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
          {error}
        </div>
      ) : null}

      {stats ? (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile label="收藏總數" value={String(stats.collection_total)} />
            <StatTile
              label="離線任務"
              value={String(stats.downloads_total)}
              sub={`已歸檔 ${stats.archived_count}`}
            />
            <StatTile
              label="歸檔率"
              value={`${(stats.archive_rate * 100).toFixed(1)}%`}
              sub="已歸檔 / 有檔案的任務"
            />
            <StatTile
              label="追蹤中"
              value={String(stats.tracked_total)}
              sub={`未讀新作 ${stats.tracked_new_total}`}
            />
          </div>

          <div className="rounded-xl border border-white/10 bg-panel p-4">
            <h2 className="mb-3 text-sm font-semibold text-white/70">雲端空間</h2>
            <div className="space-y-3">
              <QuotaBar
                label="PikPak"
                used={pikpak?.quota?.used}
                limit={pikpak?.quota?.limit}
                error={pikpak?.quota_error}
              />
              <QuotaBar
                label="pCloud"
                used={pcloud?.quota?.used}
                limit={pcloud?.quota?.limit}
                error={pcloud?.quota_error}
              />
            </div>
          </div>

          <div className="rounded-xl border border-white/10 bg-panel p-4">
            <h2 className="mb-3 text-sm font-semibold text-white/70">近 30 天活動</h2>
            <TrendBars points={stats.trend} />
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-xl border border-white/10 bg-panel p-4">
              <h2 className="mb-3 text-sm font-semibold text-white/70">收藏狀態</h2>
              <CountChips counts={stats.collection_by_status} labels={STATUS_LABELS} />
            </div>
            <div className="rounded-xl border border-white/10 bg-panel p-4">
              <h2 className="mb-3 text-sm font-semibold text-white/70">離線任務階段</h2>
              <CountChips counts={stats.downloads_by_phase} labels={PHASE_LABELS} />
            </div>
            <div className="rounded-xl border border-white/10 bg-panel p-4">
              <h2 className="mb-3 text-sm font-semibold text-white/70">pCloud 轉存</h2>
              <CountChips counts={stats.pcloud_transfers_by_status} labels={TRANSFER_LABELS} />
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            <TopList title="女優 Top 10(收藏)" items={stats.top_actresses} />
            <TopList title="類別 Top 10(收藏)" items={stats.top_genres} />
            <div className="rounded-xl border border-white/10 bg-panel p-4">
              <h2 className="mb-3 text-sm font-semibold text-white/70">追蹤新作排行</h2>
              {stats.tracked_top_new.length === 0 ? (
                <div className="text-sm text-white/40">目前沒有未讀新作</div>
              ) : (
                <ul className="space-y-2">
                  {stats.tracked_top_new.map((t) => (
                    <li key={`${t.kind}:${t.id}`} className="flex items-baseline justify-between text-sm">
                      <Link
                        href={`/${t.kind}/${encodeURIComponent(t.id)}`}
                        className="truncate hover:text-accent"
                      >
                        <span className="mr-1.5 text-xs text-white/40">
                          {TRACKED_LABELS[t.kind]}
                        </span>
                        {t.name}
                      </Link>
                      <span className="ml-2 font-mono text-accent">+{t.new_count}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className="text-right text-xs text-white/30">
            統計時間:{new Date(
              stats.built_at.endsWith("Z") ? stats.built_at : stats.built_at + "Z"
            ).toLocaleString()}
          </div>
        </>
      ) : loading ? (
        <div className="py-16 text-center text-white/40">載入中…</div>
      ) : null}
    </main>
  );
}
