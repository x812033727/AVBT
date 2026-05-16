"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import {
  api,
  type ArchiverStatus,
  type CollectionItem,
  type HistoryPage,
  type PikPakStatus,
  type PikPakTask,
  type TrackedActress,
  type TrackerStatus,
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
  return `${v.toFixed(2)} ${u[i]}`;
}

function fmtRel(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  const ms = Date.now() - d.getTime();
  if (ms < 60_000) return "剛剛";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)} 分鐘前`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)} 小時前`;
  return `${Math.floor(ms / 86_400_000)} 天前`;
}

type Stats = {
  pikpak: PikPakStatus | null;
  archiver: ArchiverStatus | null;
  tracker: TrackerStatus | null;
  collection: CollectionItem[];
  tracked: TrackedActress[];
  tasks: PikPakTask[];
  history: HistoryPage | null;
};

export default function DashboardPage() {
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
      api.get<TrackedActress[]>("/api/tracked"),
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
        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="輸入番號 / 女優 / 關鍵字"
          className="flex-1 min-w-[260px] rounded-md border border-white/10 bg-panel px-4 py-3 text-base outline-none focus:border-accent"
        />
        <button type="submit" className="btn-primary px-5 py-3">
          搜尋
        </button>
      </form>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          href="/pikpak"
          title="PikPak"
          value={
            stats.pikpak?.logged_in ? (
              <span className="text-emerald-300">✓ 已登入</span>
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
              <span className="text-accent">{runningTasks}</span>
              <span className="text-white/40"> / {stats.tasks.length}</span>
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
              <span className="text-white/40"> / {stats.collection.length}</span>
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
                <span className="ml-2 rounded bg-amber-500/20 px-2 py-0.5 text-sm text-amber-300">
                  {totalNew} 新
                </span>
              )}
            </>
          }
          sub={`位女優${stats.tracker ? `・每 ${stats.tracker.interval_seconds}s 掃一次` : ""}`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="space-y-2 rounded-lg border border-white/10 bg-panel p-4">
          <div className="flex items-center">
            <h2 className="text-sm font-semibold text-white/80">最近送出</h2>
            <Link
              href="/history"
              className="ml-auto text-xs text-white/40 hover:text-accent"
            >
              全部紀錄 →
            </Link>
          </div>
          {!stats.history?.items.length ? (
            <div className="text-sm text-white/40">還沒送過任何磁力</div>
          ) : (
            <ul className="space-y-1 text-sm">
              {stats.history.items.slice(0, 8).map((it) => (
                <li
                  key={it.id}
                  className="flex items-baseline gap-2 border-b border-white/5 py-1"
                >
                  {it.code ? (
                    <Link
                      href={`/movie/${encodeURIComponent(it.code)}`}
                      className="font-mono text-xs text-accent hover:underline"
                    >
                      {it.code}
                    </Link>
                  ) : (
                    <span className="font-mono text-xs text-white/30">-</span>
                  )}
                  <span className="truncate flex-1 text-xs text-white/70">
                    {it.name || "(未命名)"}
                  </span>
                  <span
                    className={
                      "rounded px-2 py-0.5 text-xs " +
                      (it.phase === "PHASE_TYPE_COMPLETE"
                        ? "bg-emerald-400/20 text-emerald-200"
                        : it.phase === "PHASE_TYPE_ERROR"
                        ? "bg-red-500/20 text-red-300"
                        : "bg-white/10 text-white/60")
                    }
                  >
                    {it.phase.replace("PHASE_TYPE_", "") || "—"}
                  </span>
                  {it.archived && (
                    <span className="text-xs text-emerald-300/80">📦</span>
                  )}
                  <span className="text-xs text-white/30">{fmtRel(it.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="space-y-2 rounded-lg border border-white/10 bg-panel p-4">
          <div className="flex items-center">
            <h2 className="text-sm font-semibold text-white/80">追蹤中</h2>
            <Link
              href="/tracked"
              className="ml-auto text-xs text-white/40 hover:text-accent"
            >
              管理 →
            </Link>
          </div>
          {!stats.tracked.length ? (
            <div className="text-sm text-white/40">
              還沒追蹤任何女優。到 <Link href="/search" className="text-accent">搜尋</Link> 找一個進去點「追蹤」。
            </div>
          ) : (
            <ul className="space-y-2 text-sm">
              {stats.tracked.slice(0, 8).map((t) => (
                <li key={t.id} className="flex items-center gap-2">
                  {t.avatar ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={t.avatar}
                      alt={t.name}
                      referrerPolicy="no-referrer"
                      className="h-8 w-8 flex-none rounded-full object-cover"
                    />
                  ) : (
                    <div className="grid h-8 w-8 flex-none place-items-center rounded-full bg-white/10 text-xs text-white/30">
                      ?
                    </div>
                  )}
                  <Link
                    href={`/star/${encodeURIComponent(t.id)}`}
                    className="truncate hover:text-accent"
                  >
                    {t.name || t.id}
                  </Link>
                  {t.auto_send && (
                    <span className="text-xs text-white/30">自動</span>
                  )}
                  {t.new_count > 0 && (
                    <span className="ml-auto rounded bg-amber-500/20 px-2 py-0.5 text-xs text-amber-300">
                      {t.new_count} 新
                    </span>
                  )}
                  <span className="text-xs text-white/30">
                    {fmtRel(t.last_checked_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="space-y-2 rounded-lg border border-white/10 bg-panel p-4">
          <h2 className="text-sm font-semibold text-white/80">自動歸檔</h2>
          {stats.archiver ? (
            <div className="space-y-1 text-sm text-white/70">
              <div>
                狀態：
                {stats.archiver.enabled ? (
                  <span className="text-emerald-300">啟用</span>
                ) : (
                  <span className="text-white/40">關閉</span>
                )}
                <span className="ml-2 text-xs text-white/40">
                  每 {stats.archiver.interval_seconds}s 掃一次
                </span>
              </div>
              <div className="text-xs text-white/50">
                累計歸檔 {stats.archiver.archived_total} ・ 最後執行{" "}
                {fmtRel(stats.archiver.last_run)}
              </div>
              <div className="text-xs text-white/40">
                目標：<span className="font-mono">{stats.archiver.archive_folder}/&lt;番號&gt;</span>
              </div>
              {stats.archiver.last_error && (
                <div className="text-xs text-amber-300/80">⚠ {stats.archiver.last_error}</div>
              )}
            </div>
          ) : (
            <div className="text-sm text-white/40">載入中…</div>
          )}
        </section>

        <section className="space-y-2 rounded-lg border border-white/10 bg-panel p-4">
          <h2 className="text-sm font-semibold text-white/80">女優掃描</h2>
          {stats.tracker ? (
            <div className="space-y-1 text-sm text-white/70">
              <div>
                狀態：
                {stats.tracker.enabled ? (
                  <span className="text-emerald-300">啟用</span>
                ) : (
                  <span className="text-white/40">關閉</span>
                )}
                <span className="ml-2 text-xs text-white/40">
                  每 {stats.tracker.interval_seconds}s 掃一次
                </span>
              </div>
              <div className="text-xs text-white/50">
                最後執行 {fmtRel(stats.tracker.last_run)} ・ 那次找到{" "}
                {stats.tracker.last_new_total} 部新作品
              </div>
              {stats.tracker.last_error && (
                <div className="text-xs text-amber-300/80">⚠ {stats.tracker.last_error}</div>
              )}
            </div>
          ) : (
            <div className="text-sm text-white/40">載入中…</div>
          )}
        </section>
      </div>
    </div>
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
      className="block rounded-lg border border-white/10 bg-panel p-4 transition hover:border-accent/50"
    >
      <div className="text-xs uppercase tracking-wide text-white/40">{title}</div>
      <div className="mt-1 text-2xl font-bold">{value}</div>
      <div className="mt-1 truncate text-xs text-white/50">{sub}</div>
    </Link>
  );
}
