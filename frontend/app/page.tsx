"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { Archive, Check, Search, TriangleAlert } from "lucide-react";
import {
  api,
  imgProxy,
  type ArchiverStatus,
  type CollectionItem,
  type HistoryPage,
  type PikPakStatus,
  type PikPakTask,
  type TrackedListing,
  type TrackerStatus,
} from "@/lib/api";
import { fmtBytes, fmtRel } from "@/lib/format";
import { pikpakPhaseTone } from "@/lib/status";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type Stats = {
  pikpak: PikPakStatus | null;
  archiver: ArchiverStatus | null;
  tracker: TrackerStatus | null;
  collection: CollectionItem[];
  tracked: TrackedListing[];
  tasks: PikPakTask[];
  history: HistoryPage | null;
};

export default function HomePage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [stats, setStats] = useState<Stats>({
    pikpak: null,
    archiver: null,
    tracker: null,
    collection: [],
    tracked: [],
    tasks: [],
    history: null,
  });

  useEffect(() => {
    let alive = true;
    Promise.allSettled([
      api.get<PikPakStatus>("/api/pikpak/status"),
      api.get<ArchiverStatus>("/api/pikpak/archiver"),
      api.get<TrackerStatus>("/api/tracked/status"),
      api.get<CollectionItem[]>("/api/collection"),
      api.get<TrackedListing[]>("/api/tracked"),
      api.get<PikPakTask[]>("/api/pikpak/tasks"),
      api.get<HistoryPage>("/api/collection/history?limit=10"),
    ]).then((rs) => {
      if (!alive) return;
      const val = <T,>(r: PromiseSettledResult<T>, def: T): T =>
        r.status === "fulfilled" ? r.value : def;
      setStats({
        pikpak: val(rs[0] as any, null),
        archiver: val(rs[1] as any, null),
        tracker: val(rs[2] as any, null),
        collection: val(rs[3] as any, []),
        tracked: val(rs[4] as any, []),
        tasks: val(rs[5] as any, []),
        history: val(rs[6] as any, null),
      });
    });
    return () => {
      alive = false;
    };
  }, []);

  function search(e: FormEvent) {
    e.preventDefault();
    if (!q.trim()) return;
    router.push(`/search?q=${encodeURIComponent(q.trim())}`);
  }

  const wishlistCount = stats.collection.filter((c) => c.status === "wishlist").length;
  const downloadingCount = stats.collection.filter((c) => c.status === "downloading").length;
  const doneCount = stats.collection.filter((c) => c.status === "done").length;
  const runningTasks = stats.tasks.filter((t) =>
    ["PHASE_TYPE_PENDING", "PHASE_TYPE_RUNNING", "PHASE_TYPE_QUEUED"].includes(t.phase)
  ).length;
  const failedTasks = stats.tasks.filter((t) => t.phase === "PHASE_TYPE_ERROR").length;
  const totalNew = stats.tracked.reduce((s, t) => s + (t.new_count || 0), 0);

  return (
    <div className="space-y-6">
      <form onSubmit={search} className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[260px] flex-1">
          <Search
            className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="輸入番號 / 女優 / 關鍵字"
            className="h-12 pl-10 text-base"
          />
        </div>
        <Button type="submit" size="lg" className="h-12 px-6">
          搜尋
        </Button>
      </form>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          href="/pikpak"
          title="PikPak"
          value={
            stats.pikpak?.logged_in ? (
              <span className="inline-flex items-center gap-1.5 text-emerald-300">
                <Check className="h-5 w-5" aria-hidden />
                已登入
              </span>
            ) : (
              <span className="text-amber-300">未登入</span>
            )
          }
          sub={
            stats.pikpak?.quota
              ? `${fmtBytes(stats.pikpak.quota.used)} / ${fmtBytes(stats.pikpak.quota.limit)}`
              : "未連線"
          }
        />
        <Tile
          href="/pikpak"
          title="離線任務"
          value={
            <>
              <span className="text-primary">{runningTasks}</span>
              <span className="text-muted-foreground"> / {stats.tasks.length}</span>
            </>
          }
          sub={`${failedTasks} 失敗 ・ ${stats.tasks.length - runningTasks - failedTasks} 完成`}
        />
        <Tile
          href="/collection"
          title="收藏"
          value={
            <>
              <span>{wishlistCount}</span>
              <span className="text-muted-foreground"> / {stats.collection.length}</span>
            </>
          }
          sub={`待看 ${wishlistCount} ・ 下載中 ${downloadingCount} ・ 完成 ${doneCount}`}
        />
        <Tile
          href="/tracked"
          title="追蹤"
          value={
            <>
              <span>{stats.tracked.length}</span>
              {totalNew > 0 && (
                <StatusBadge tone="warning" className="ml-2 align-middle text-sm">
                  {totalNew} 新
                </StatusBadge>
              )}
            </>
          }
          sub={`位女優${stats.tracker ? `・每 ${stats.tracker.interval_seconds}s 掃一次` : ""}`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="space-y-2 rounded-lg border border-border bg-card p-4">
          <div className="flex items-center">
            <h2 className="text-sm font-semibold text-foreground/80">最近送出</h2>
            <Link
              href="/history"
              className="ml-auto text-xs text-muted-foreground transition hover:text-primary"
            >
              全部紀錄 →
            </Link>
          </div>
          {!stats.history?.items.length ? (
            <div className="text-sm text-muted-foreground">還沒送過任何磁力</div>
          ) : (
            <ul className="space-y-1 text-sm">
              {stats.history.items.slice(0, 8).map((it) => {
                const phase = pikpakPhaseTone(it.phase);
                return (
                  <li
                    key={it.id}
                    className="flex items-baseline gap-2 border-b border-border/50 py-1"
                  >
                    {it.code ? (
                      <Link
                        href={`/movie/${encodeURIComponent(it.code)}`}
                        className="font-mono text-xs text-primary hover:underline"
                      >
                        {it.code}
                      </Link>
                    ) : (
                      <span className="font-mono text-xs text-muted-foreground/50">-</span>
                    )}
                    <span className="min-w-0 flex-1 truncate text-xs text-foreground/70">
                      {it.name || "(未命名)"}
                    </span>
                    <StatusBadge tone={phase.tone}>{phase.label}</StatusBadge>
                    {it.archived && (
                      <Archive
                        className="h-3.5 w-3.5 self-center text-emerald-300/80"
                        aria-label="已歸檔"
                      />
                    )}
                    <span className="text-xs text-muted-foreground/70">
                      {fmtRel(it.created_at)}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <section className="space-y-2 rounded-lg border border-border bg-card p-4">
          <div className="flex items-center">
            <h2 className="text-sm font-semibold text-foreground/80">追蹤中</h2>
            <Link
              href="/tracked"
              className="ml-auto text-xs text-muted-foreground transition hover:text-primary"
            >
              管理 →
            </Link>
          </div>
          {!stats.tracked.length ? (
            <div className="text-sm text-muted-foreground">
              還沒追蹤任何女優。到{" "}
              <Link href="/search" className="text-primary">
                搜尋
              </Link>{" "}
              找一個進去點「追蹤」。
            </div>
          ) : (
            <ul className="space-y-2 text-sm">
              {stats.tracked.slice(0, 8).map((t) => (
                <li key={t.id} className="flex items-center gap-2">
                  {t.avatar ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={imgProxy(t.avatar)}
                      alt={t.name}
                      loading="lazy"
                      referrerPolicy="no-referrer"
                      className="h-8 w-8 flex-none rounded-full object-cover"
                    />
                  ) : (
                    <div className="grid h-8 w-8 flex-none place-items-center rounded-full bg-muted text-xs text-muted-foreground/60">
                      ?
                    </div>
                  )}
                  <Link
                    href={`/star/${encodeURIComponent(t.id)}`}
                    className="truncate transition hover:text-primary"
                  >
                    {t.name || t.id}
                  </Link>
                  {t.auto_send && (
                    <span className="text-xs text-muted-foreground/70">自動</span>
                  )}
                  {t.new_count > 0 && (
                    <StatusBadge tone="warning" className="ml-auto">
                      {t.new_count} 新
                    </StatusBadge>
                  )}
                  <span className="text-xs text-muted-foreground/70">
                    {fmtRel(t.last_checked_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <StatusSection
          title="自動歸檔"
          data={stats.archiver}
          renderBody={(a) => (
            <>
              <div>
                狀態:
                {a.enabled ? (
                  <span className="text-emerald-300">啟用</span>
                ) : (
                  <span className="text-muted-foreground">關閉</span>
                )}
                <span className="ml-2 text-xs text-muted-foreground">
                  每 {a.interval_seconds}s 掃一次
                </span>
              </div>
              <div className="text-xs text-muted-foreground">
                累計歸檔 {a.archived_total} ・ 最後執行 {fmtRel(a.last_run)}
              </div>
              <div className="text-xs text-muted-foreground/80">
                目標:<span className="font-mono">{a.archive_folder}/&lt;番號&gt;</span>
              </div>
              {a.last_error && (
                <div className="flex items-center gap-1 text-xs text-amber-300/90">
                  <TriangleAlert className="h-3.5 w-3.5" aria-hidden />
                  {a.last_error}
                </div>
              )}
            </>
          )}
        />

        <StatusSection
          title="女優掃描"
          data={stats.tracker}
          renderBody={(t) => (
            <>
              <div>
                狀態:
                {t.enabled ? (
                  <span className="text-emerald-300">啟用</span>
                ) : (
                  <span className="text-muted-foreground">關閉</span>
                )}
                <span className="ml-2 text-xs text-muted-foreground">
                  每 {t.interval_seconds}s 掃一次
                </span>
              </div>
              <div className="text-xs text-muted-foreground">
                最後執行 {fmtRel(t.last_run)} ・ 那次找到 {t.last_new_total} 部新作品
              </div>
              {t.last_error && (
                <div className="flex items-center gap-1 text-xs text-amber-300/90">
                  <TriangleAlert className="h-3.5 w-3.5" aria-hidden />
                  {t.last_error}
                </div>
              )}
            </>
          )}
        />
      </div>
    </div>
  );
}

function StatusSection<T>({
  title,
  data,
  renderBody,
}: {
  title: string;
  data: T | null;
  renderBody: (data: T) => React.ReactNode;
}) {
  return (
    <section className="space-y-2 rounded-lg border border-border bg-card p-4">
      <h2 className="text-sm font-semibold text-foreground/80">{title}</h2>
      {data ? (
        <div className="space-y-1 text-sm text-foreground/70">{renderBody(data)}</div>
      ) : (
        <div className="text-sm text-muted-foreground">載入中…</div>
      )}
    </section>
  );
}

function Tile({
  href,
  title,
  value,
  sub,
}: {
  href: string;
  title: string;
  value: React.ReactNode;
  sub: string;
}) {
  return (
    <Link
      href={href}
      className="block rounded-lg border border-border bg-card p-4 transition hover:border-primary/50"
    >
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{title}</div>
      <div className="mt-1 text-2xl font-bold tabular-nums">{value}</div>
      <div className="mt-1 truncate text-xs text-muted-foreground">{sub}</div>
    </Link>
  );
}
