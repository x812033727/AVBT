"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import PCloudCleanupButton from "@/components/PCloudCleanupButton";
import PCloudOrganizeButton from "@/components/PCloudOrganizeButton";
import PCloudMoveModal from "@/components/PCloudMoveModal";
import { confirmDialog, toast } from "@/components/Toast";
import {
  api,
  type PCloudFile,
  type PCloudFolderStats,
  type PCloudQuota,
  type PCloudQueueStatus,
  type PCloudStatus,
  type PCloudTransfer,
  type PCloudTransferPage,
} from "@/lib/api";
import { isVideo } from "@/lib/video";

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

// Status pill colors for the transfer queue rows.
const STATUS_COLORS: Record<string, string> = {
  pending: "bg-white/10 text-white/70",
  running: "bg-blue-500/20 text-blue-200",
  done: "bg-emerald-400/20 text-emerald-200",
  failed: "bg-red-500/20 text-red-300",
  cancelled: "bg-white/5 text-white/40",
};

const STATUS_FILTERS = [
  { key: "", label: "全部" },
  { key: "pending", label: "等待中" },
  { key: "running", label: "傳輸中" },
  { key: "done", label: "完成" },
  { key: "failed", label: "失敗" },
  { key: "cancelled", label: "已取消" },
] as const;

export default function PCloudPage() {
  const [tab, setTab] = useState<"files" | "transfers">("files");
  const [status, setStatus] = useState<PCloudStatus | null>(null);

  const reloadStatus = useCallback(async () => {
    try {
      const s = await api.get<PCloudStatus>("/api/pcloud/status");
      setStatus(s);
    } catch {
      // status endpoint may fail if backend down; surface in the panel instead
    }
  }, []);

  useEffect(() => {
    reloadStatus();
  }, [reloadStatus]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1">
          <button
            onClick={() => setTab("files")}
            className={tab === "files" ? "btn-primary" : "btn-ghost"}
          >
            雲端檔案
          </button>
          <button
            onClick={() => setTab("transfers")}
            className={tab === "transfers" ? "btn-primary" : "btn-ghost"}
          >
            PikPak 傳輸佇列
          </button>
        </div>
        {status && !status.logged_in && (
          <span className="ml-2 text-xs text-amber-300/80">
            未登入 — 部分功能需先登入 pCloud
          </span>
        )}
      </div>

      {!status?.logged_in && <LoginPanel status={status} onChanged={reloadStatus} />}

      {tab === "files" && <FilesTab loggedIn={!!status?.logged_in} />}
      {tab === "transfers" && <TransfersTab loggedIn={!!status?.logged_in} />}
    </div>
  );
}

// ---------- 雲端檔案 (sourced from main) ----------

function FilesTab({ loggedIn }: { loggedIn: boolean }) {
  const [quota, setQuota] = useState<PCloudQuota | null>(null);
  const [files, setFiles] = useState<PCloudFile[]>([]);
  const [parents, setParents] = useState<{ id: string; name: string }[]>([
    { id: "0", name: "我的 pCloud" },
  ]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const currentParent = parents[parents.length - 1].id;
  const currentName = parents[parents.length - 1].name;
  const atRoot = parents.length <= 1;

  const loadFiles = useCallback(async (parentId: string) => {
    setError(null);
    setLoading(true);
    try {
      const [list, q] = await Promise.all([
        api.get<PCloudFile[]>(
          `/api/pcloud/files?parent_id=${encodeURIComponent(parentId)}`
        ),
        api.get<PCloudQuota>("/api/pcloud/quota").catch(() => null),
      ]);
      setFiles(list);
      setQuota(q);
    } catch (e: any) {
      setError(e.message);
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (loggedIn) loadFiles(currentParent);
  }, [currentParent, loadFiles, loggedIn]);

  async function openItem(f: PCloudFile) {
    if (f.kind === "folder") {
      setSearch("");
      setParents([...parents, { id: f.id, name: f.name }]);
      return;
    }
    try {
      const { url } = await api.get<{ url: string }>(
        `/api/pcloud/files/${f.id}/url`
      );
      if (url) window.open(url, "_blank");
    } catch (e: any) {
      toast.error(e.message || "讀取連結失敗");
    }
  }

  function gotoCrumb(idx: number) {
    setSearch("");
    setParents(parents.slice(0, idx + 1));
  }

  async function trashItems(ids: string[]) {
    if (!ids.length) return;
    const ok = await confirmDialog(
      `刪除 ${ids.length} 個項目？`,
      "資料夾將連同內容一起刪除"
    );
    if (!ok) return;
    try {
      await api.post("/api/pcloud/files/trash", { ids });
      toast.success(`已刪除 ${ids.length} 個項目`);
      loadFiles(currentParent);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function renameItem(f: PCloudFile) {
    const next = window.prompt("新名稱", f.name);
    if (!next || next === f.name) return;
    try {
      await api.post("/api/pcloud/files/rename", {
        file_id: f.id,
        new_name: next,
      });
      toast.success("已改名");
      loadFiles(currentParent);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function createFolder() {
    const name = window.prompt("新資料夾名稱");
    if (!name) return;
    try {
      await api.post("/api/pcloud/folders/create", {
        parent_id: currentParent,
        name,
      });
      toast.success(`已建立資料夾 ${name}`);
      loadFiles(currentParent);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function runSearch() {
    if (!search.trim()) {
      loadFiles(currentParent);
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const res = await api.get<PCloudFile[]>(
        `/api/pcloud/files/search?q=${encodeURIComponent(
          search.trim()
        )}&parent_id=${encodeURIComponent(currentParent)}`
      );
      setFiles(res);
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }

  if (!loggedIn) return null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => loadFiles(currentParent)} className="btn-ghost">
          {loading ? "更新中…" : "重新整理"}
        </button>
        <button className="btn-ghost" onClick={createFolder}>
          ➕ 新增資料夾
        </button>
        {quota && (
          <div className="ml-auto text-xs text-white/50">
            已用 {fmtBytes(quota.used)} / {fmtBytes(quota.limit)}
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      <FilesPanel
        files={files}
        parents={parents}
        currentParent={currentParent}
        currentName={currentName}
        atRoot={atRoot}
        search={search}
        onSearch={setSearch}
        onSubmitSearch={runSearch}
        onOpen={openItem}
        onRename={renameItem}
        onCrumb={gotoCrumb}
        onTrash={trashItems}
        onRefresh={() => loadFiles(currentParent)}
      />
    </div>
  );
}

function FilesPanel({
  files,
  parents,
  currentParent,
  currentName,
  atRoot,
  search,
  onSearch,
  onSubmitSearch,
  onOpen,
  onRename,
  onCrumb,
  onTrash,
  onRefresh,
}: {
  files: PCloudFile[];
  parents: { id: string; name: string }[];
  currentParent: string;
  currentName: string;
  atRoot: boolean;
  search: string;
  onSearch: (s: string) => void;
  onSubmitSearch: () => void;
  onOpen: (f: PCloudFile) => void;
  onRename: (f: PCloudFile) => void;
  onCrumb: (idx: number) => void;
  onTrash: (ids: string[]) => void;
  onRefresh: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [moveOpen, setMoveOpen] = useState(false);

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  const selectedIds = Array.from(selected);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap items-center gap-1 text-sm text-white/60">
          {parents.map((p, i) => (
            <span key={p.id + i} className="flex items-center gap-1">
              {i > 0 && <span className="text-white/30">/</span>}
              <button className="hover:text-accent" onClick={() => onCrumb(i)}>
                {p.name}
              </button>
            </span>
          ))}
        </div>
        <div className="ml-auto flex gap-2">
          <PCloudOrganizeButton
            folder_id={currentParent}
            folder_name={currentName}
            disabled={atRoot}
            onDone={onRefresh}
          />
          <PCloudCleanupButton
            folder_id={currentParent}
            folder_name={currentName}
            disabled={atRoot}
            onDone={onRefresh}
          />
        </div>
        <form
          className="flex gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            onSubmitSearch();
          }}
        >
          <input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="搜尋此目錄的檔案"
            className="w-56 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
          />
          <button type="submit" className="btn-ghost">
            搜尋
          </button>
        </form>
      </div>

      <PCloudFolderStatsBar parentId={currentParent} />

      {selectedIds.length > 0 && (
        <div className="flex gap-2 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
          <span className="text-white/60">已選 {selectedIds.length} 個</span>
          <button
            className="ml-auto text-amber-300 hover:underline"
            onClick={() => setMoveOpen(true)}
          >
            移動到…
          </button>
          <button
            className="text-red-300 hover:underline"
            onClick={() => onTrash(selectedIds)}
          >
            刪除
          </button>
        </div>
      )}

      <PCloudMoveModal
        open={moveOpen}
        fileIds={selectedIds}
        onClose={() => setMoveOpen(false)}
        onDone={() => {
          setSelected(new Set());
          onRefresh();
        }}
      />

      {!files.length ? (
        <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
          此資料夾為空
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-white/10">
          <table className="w-full text-sm">
            <thead className="bg-white/5 text-left text-xs uppercase tracking-wide text-white/40">
              <tr>
                <th className="px-3 py-2 w-10"></th>
                <th className="px-3 py-2">名稱</th>
                <th className="px-3 py-2 w-24">類型</th>
                <th className="px-3 py-2 w-28">大小</th>
                <th className="px-3 py-2 w-32">操作</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.id} className="border-t border-white/5">
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={selected.has(f.id)}
                      onChange={() => toggle(f.id)}
                      className="h-4 w-4 accent-accent"
                    />
                  </td>
                  <td className="px-3 py-2">
                    <button
                      className="text-left text-white/90 hover:text-accent"
                      onClick={() => onOpen(f)}
                    >
                      {f.kind === "folder"
                        ? "📁 "
                        : isVideo(f.name)
                        ? "▶ "
                        : "📄 "}
                      {f.name}
                    </button>
                  </td>
                  <td className="px-3 py-2 text-white/60">
                    {f.kind === "folder" ? "資料夾" : "檔案"}
                  </td>
                  <td className="px-3 py-2 text-white/70">{fmtBytes(f.size)}</td>
                  <td className="px-3 py-2">
                    <div className="flex gap-2 text-xs">
                      <button
                        onClick={() => onRename(f)}
                        className="text-cyan-300 hover:underline"
                      >
                        改名
                      </button>
                      <button
                        onClick={() => onTrash([f.id])}
                        className="text-red-300 hover:underline"
                      >
                        刪除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PCloudFolderStatsBar({ parentId }: { parentId: string }) {
  const [stats, setStats] = useState<PCloudFolderStats | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<PCloudFolderStats>(
        `/api/pcloud/files/stats?parent_id=${encodeURIComponent(parentId)}`
      )
      .then((res) => {
        if (alive) setStats(res);
      })
      .catch(() => {
        if (alive) setStats(null);
      });
    return () => {
      alive = false;
    };
  }, [parentId]);

  if (!stats || (stats.total_files === 0 && stats.total_folders === 0)) {
    return null;
  }

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-white/5 bg-white/[0.03] px-3 py-1.5 text-xs text-white/60">
      <span>
        <span className="text-white/40">檔案</span> {stats.total_files} ·
        <span className="ml-1 text-white/40">資料夾</span> {stats.total_folders}
      </span>
      <span>
        <span className="text-white/40">總大小</span> {fmtBytes(stats.total_size)}
      </span>
      {stats.video_count > 0 && (
        <span>
          <span className="text-white/40">影片</span> {stats.video_count} (
          {fmtBytes(stats.video_size)})
        </span>
      )}
      {stats.coded_count > 0 && (
        <span>
          <span className="text-white/40">有番號</span> {stats.coded_count}
        </span>
      )}
      {stats.partial && <span className="text-amber-300/80">(部分統計)</span>}
    </div>
  );
}

// ---------- PikPak 傳輸佇列 ----------

function TransfersTab({ loggedIn }: { loggedIn: boolean }) {
  const [queue, setQueue] = useState<PCloudQueueStatus | null>(null);
  const [page, setPage] = useState<PCloudTransferPage | null>(null);
  const [filter, setFilter] = useState<string>("");
  const [auto, setAuto] = useState(true);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [q, p] = await Promise.all([
        api.get<PCloudQueueStatus>("/api/pcloud/queue"),
        api.get<PCloudTransferPage>(
          `/api/pcloud/transfers?limit=200${filter ? `&status=${filter}` : ""}`
        ),
      ]);
      setQueue(q);
      setPage(p);
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const hasActive = !!(queue && (queue.pending > 0 || queue.running > 0));
  const timerRef = useRef<number | null>(null);
  useEffect(() => {
    if (!auto || !hasActive) return;
    timerRef.current = window.setTimeout(refresh, 5000);
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [auto, hasActive, page, refresh]);

  async function retry(id: number) {
    try {
      await api.post(`/api/pcloud/transfers/${id}/retry`);
      toast.success("已重新排入佇列");
      refresh();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function cancel(id: number) {
    try {
      await api.post(`/api/pcloud/transfers/${id}/cancel`);
      toast.success("已取消");
      refresh();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function cleanup(keepFailed: boolean) {
    const verb = keepFailed ? "已完成 + 已取消" : "已完成 + 已取消 + 失敗";
    const ok = await confirmDialog(`清掉 ${verb} 的紀錄?`);
    if (!ok) return;
    try {
      const res = await api.post<{ deleted: number }>(
        "/api/pcloud/transfers/cleanup",
        { keep_failed: keepFailed }
      );
      toast.success(`已刪除 ${res.deleted} 筆`);
      refresh();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={refresh} className="btn-ghost" disabled={!loggedIn}>
          {loading ? "更新中…" : "重新整理"}
        </button>
        <label className="flex items-center gap-1 text-xs text-white/60">
          <input
            type="checkbox"
            checked={auto}
            onChange={(e) => setAuto(e.target.checked)}
          />
          有任務時自動更新 (5s)
        </label>
        {queue && (
          <div className="ml-auto text-xs text-white/50">
            佇列 {queue.running} / {queue.concurrency} ・ 排隊 {queue.pending}
          </div>
        )}
      </div>

      <QueueBar queue={queue} onCleanup={cleanup} />

      <div className="flex flex-wrap gap-1">
        {STATUS_FILTERS.map((s) => (
          <button
            key={s.key || "all"}
            onClick={() => setFilter(s.key)}
            className={
              filter === s.key ? "btn-primary text-xs" : "btn-ghost text-xs"
            }
          >
            {s.label}
            {page && s.key && (
              <span className="ml-1 text-white/40">
                ({(page as any)[s.key] ?? 0})
              </span>
            )}
          </button>
        ))}
      </div>

      <TransfersTable
        items={page?.items ?? []}
        onRetry={retry}
        onCancel={cancel}
      />
    </div>
  );
}

function QueueBar({
  queue,
  onCleanup,
}: {
  queue: PCloudQueueStatus | null;
  onCleanup: (keepFailed: boolean) => void;
}) {
  if (!queue) return null;
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-xs text-white/70">
      <span>排隊 {queue.pending}</span>
      <span className="text-white/40">|</span>
      <span>傳輸中 {queue.running}</span>
      <span className="text-white/40">|</span>
      <span className="text-emerald-300/80">完成 {queue.done}</span>
      <span className="text-white/40">|</span>
      <span className="text-red-300/80">失敗 {queue.failed}</span>
      <span className="text-white/40">|</span>
      <span className="text-white/50">
        併發上限 {queue.concurrency}・本機已送出 {queue.inflight}
      </span>
      <button
        onClick={() => onCleanup(true)}
        className="ml-auto rounded border border-white/10 px-2 py-0.5 text-white/70 hover:bg-white/10"
        title="清掉 已完成 + 已取消"
      >
        清掉完成
      </button>
      <button
        onClick={() => onCleanup(false)}
        className="rounded border border-red-500/30 px-2 py-0.5 text-red-300 hover:bg-red-500/10"
        title="清掉 已完成 + 已取消 + 失敗"
      >
        清掉所有結束項
      </button>
    </div>
  );
}

function TransfersTable({
  items,
  onRetry,
  onCancel,
}: {
  items: PCloudTransfer[];
  onRetry: (id: number) => void;
  onCancel: (id: number) => void;
}) {
  // Group rows by destination folder for visual grouping.
  const groups = useMemo(() => {
    const m = new Map<string, PCloudTransfer[]>();
    for (const it of items) {
      const k = it.pcloud_folder_path || "/";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(it);
    }
    return Array.from(m.entries());
  }, [items]);

  if (!items.length) {
    return (
      <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
        沒有傳輸任務 — 到 /pikpak 頁勾選檔案後按「→ pCloud」開始
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {groups.map(([path, rows]) => (
        <div key={path} className="overflow-hidden rounded-lg border border-white/10">
          <div className="flex items-center justify-between border-b border-white/10 bg-white/5 px-3 py-2 text-xs">
            <span className="font-mono text-white/70">{path}</span>
            <span className="text-white/40">{rows.length} 個檔案</span>
          </div>
          <table className="w-full text-sm">
            <thead className="bg-white/[0.02] text-left text-xs uppercase tracking-wide text-white/40">
              <tr>
                <th className="px-3 py-2">檔名</th>
                <th className="px-3 py-2 w-24">狀態</th>
                <th className="px-3 py-2 w-40">進度</th>
                <th className="px-3 py-2 w-24">大小</th>
                <th className="px-3 py-2 w-28">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const pct = r.pikpak_size
                  ? Math.min(
                      100,
                      Math.round((r.bytes_downloaded / r.pikpak_size) * 100)
                    )
                  : 0;
                return (
                  <tr key={r.id} className="border-t border-white/5 align-top">
                    <td className="px-3 py-2">
                      <div className="truncate text-white/90">
                        {r.pikpak_name || `(file_id ${r.pikpak_file_id})`}
                      </div>
                      {r.pikpak_path && (
                        <div className="text-xs text-white/40">
                          來源子路徑: {r.pikpak_path}
                        </div>
                      )}
                      {r.message && (
                        <div className="text-xs text-white/40">{r.message}</div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={
                          "rounded px-2 py-0.5 text-xs " +
                          (STATUS_COLORS[r.status] || "bg-white/10")
                        }
                      >
                        {r.status}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      {r.status === "running" && r.pikpak_size > 0 ? (
                        <div className="space-y-0.5">
                          <div className="h-1.5 w-full overflow-hidden rounded bg-white/10">
                            <div
                              className="h-full bg-accent"
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                          <div className="text-xs text-white/50">
                            {fmtBytes(r.bytes_downloaded)} /{" "}
                            {fmtBytes(r.pikpak_size)} ({pct}%)
                          </div>
                        </div>
                      ) : r.status === "done" ? (
                        <span className="text-xs text-emerald-300/80">100%</span>
                      ) : (
                        <span className="text-xs text-white/40">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-white/70">
                      {fmtBytes(r.pikpak_size)}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex gap-2 text-xs">
                        {(r.status === "failed" || r.status === "cancelled") && (
                          <button
                            onClick={() => onRetry(r.id)}
                            className="text-amber-300 hover:underline"
                          >
                            重試
                          </button>
                        )}
                        {(r.status === "pending" || r.status === "running") && (
                          <button
                            onClick={() => onCancel(r.id)}
                            className="text-red-300 hover:underline"
                          >
                            取消
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

// ---------- 登入面板 ----------

function LoginPanel({
  status,
  onChanged,
}: {
  status: PCloudStatus | null;
  onChanged: () => void;
}) {
  const [mode, setMode] = useState<"password" | "token">("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Sticky in-form error: toasts auto-dismiss but the multi-line
  // diagnostic message from /api/pcloud/login needs to stay visible long
  // enough for the user to read all three possible causes.
  const [loginError, setLoginError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setLoginError(null);
    try {
      const body =
        mode === "token"
          ? { access_token: token.trim() }
          : { username: username.trim(), password };
      await api.post("/api/pcloud/login", body);
      toast.success("pCloud 登入成功");
      setUsername("");
      setPassword("");
      setToken("");
      onChanged();
    } catch (e: any) {
      const msg = e?.message || "登入失敗";
      setLoginError(msg);
      toast.error(msg.split("\n")[0]);
    } finally {
      setSubmitting(false);
    }
  }

  if (!status) return null;

  return (
    <div className="rounded-md border border-white/10 bg-white/5 px-4 py-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-amber-300">●</span>
        <span className="text-white/80">尚未登入 pCloud</span>
        {(status.has_env_credentials || status.has_env_token) && (
          <span className="text-xs text-white/40">
            (.env 已設定 — 首次呼叫 API 會自動登入)
          </span>
        )}
      </div>
      <form onSubmit={submit} className="space-y-2">
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => setMode("password")}
            className={mode === "password" ? "btn-primary text-xs" : "btn-ghost text-xs"}
          >
            帳密
          </button>
          <button
            type="button"
            onClick={() => setMode("token")}
            className={mode === "token" ? "btn-primary text-xs" : "btn-ghost text-xs"}
          >
            Access Token
          </button>
        </div>
        {mode === "password" ? (
          <div className="flex flex-wrap gap-2">
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="pCloud Email"
              className="flex-1 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
              autoComplete="username"
            />
            <input
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="密碼"
              type="password"
              className="flex-1 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
              autoComplete="current-password"
            />
          </div>
        ) : (
          <input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="貼上 pCloud access token"
            className="w-full rounded-md border border-white/10 bg-panel px-2 py-1 font-mono text-sm outline-none focus:border-accent"
          />
        )}
        <button type="submit" className="btn-primary text-sm" disabled={submitting}>
          {submitting ? "登入中…" : "登入"}
        </button>
        {loginError && (
          <div className="whitespace-pre-wrap rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs leading-relaxed text-red-300">
            {loginError}
          </div>
        )}
      </form>
    </div>
  );
}
