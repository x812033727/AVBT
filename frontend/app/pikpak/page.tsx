"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import CleanupButton from "@/components/CleanupButton";
import {
  api,
  type ArchiverStatus,
  type PikPakFile,
  type PikPakQuota,
  type PikPakTask,
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

const ACTIVE_PHASES = new Set([
  "PHASE_TYPE_PENDING",
  "PHASE_TYPE_RUNNING",
  "PHASE_TYPE_QUEUED",
]);

export default function PikpakPage() {
  const [tab, setTab] = useState<"tasks" | "files">("tasks");
  const [quota, setQuota] = useState<PikPakQuota | null>(null);
  const [tasks, setTasks] = useState<PikPakTask[]>([]);
  const [files, setFiles] = useState<PikPakFile[]>([]);
  const [parents, setParents] = useState<{ id: string; name: string }[]>([
    { id: "", name: "我的雲盤" },
  ]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const [archiver, setArchiver] = useState<ArchiverStatus | null>(null);

  const loadTasks = useCallback(async () => {
    setError(null);
    try {
      const [q, t, a] = await Promise.all([
        api.get<PikPakQuota>("/api/pikpak/quota").catch(() => null),
        api.get<PikPakTask[]>("/api/pikpak/tasks"),
        api.get<ArchiverStatus>("/api/pikpak/archiver").catch(() => null),
      ]);
      setQuota(q);
      setTasks(t);
      setArchiver(a);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  async function toggleArchiver(enabled: boolean) {
    try {
      const a = await api.post<ArchiverStatus>("/api/pikpak/archiver/toggle", {
        enabled,
      });
      setArchiver(a);
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function runArchiverNow() {
    try {
      const a = await api.post<ArchiverStatus & { moved: number }>(
        "/api/pikpak/archiver/run"
      );
      setArchiver(a);
      if (a.moved) loadTasks();
    } catch (e: any) {
      setError(e.message);
    }
  }

  const currentParent = parents[parents.length - 1].id;

  const loadFiles = useCallback(
    async (parentId: string) => {
      setError(null);
      setLoading(true);
      try {
        const res = await api.get<PikPakFile[]>(
          `/api/pikpak/files?parent_id=${encodeURIComponent(parentId)}`
        );
        setFiles(res);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    if (tab === "tasks") loadTasks();
    else loadFiles(currentParent);
  }, [tab, currentParent, loadTasks, loadFiles]);

  // Auto-refresh tasks every 8s if any task is still running.
  const timerRef = useRef<number | null>(null);
  useEffect(() => {
    if (tab !== "tasks" || !auto) return;
    const hasActive = tasks.some((t) => ACTIVE_PHASES.has(t.phase));
    if (!hasActive) return;
    timerRef.current = window.setTimeout(loadTasks, 8000);
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [tab, tasks, auto, loadTasks]);

  async function openFolder(f: PikPakFile) {
    if (f.kind !== "drive#folder") {
      const { url } = await api.get<{ url: string }>(
        `/api/pikpak/files/${f.id}/url`
      );
      if (url) window.open(url, "_blank");
      return;
    }
    setSearch("");
    setParents([...parents, { id: f.id, name: f.name }]);
  }

  function gotoCrumb(idx: number) {
    setSearch("");
    setParents(parents.slice(0, idx + 1));
  }

  async function deleteTasks(ids: string[]) {
    if (!ids.length) return;
    if (!confirm(`刪除 ${ids.length} 個任務？`)) return;
    await api.post("/api/pikpak/tasks/delete", {
      task_ids: ids,
      delete_files: false,
    });
    loadTasks();
  }

  async function retryTask(id: string) {
    try {
      await api.post(`/api/pikpak/tasks/${id}/retry`);
      loadTasks();
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function trashFiles(ids: string[]) {
    if (!ids.length) return;
    if (!confirm(`移到垃圾桶 ${ids.length} 個檔案？`)) return;
    await api.post("/api/pikpak/files/trash", { ids });
    loadFiles(currentParent);
  }

  async function shareFiles(ids: string[]) {
    try {
      const res = await api.post<{ url: string; pass_code: string }>(
        "/api/pikpak/share",
        { file_ids: ids }
      );
      if (res.url) {
        await navigator.clipboard.writeText(
          res.url + (res.pass_code ? ` (碼: ${res.pass_code})` : "")
        );
        alert(`分享連結已複製到剪貼簿：\n${res.url}`);
      }
    } catch (e: any) {
      setError(e.message);
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
      const res = await api.get<PikPakFile[]>(
        `/api/pikpak/files/search?q=${encodeURIComponent(search.trim())}&parent_id=${encodeURIComponent(currentParent)}`
      );
      setFiles(res);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1">
          <button
            onClick={() => setTab("tasks")}
            className={tab === "tasks" ? "btn-primary" : "btn-ghost"}
          >
            離線任務
          </button>
          <button
            onClick={() => setTab("files")}
            className={tab === "files" ? "btn-primary" : "btn-ghost"}
          >
            雲端檔案
          </button>
        </div>
        <button
          onClick={() => (tab === "tasks" ? loadTasks() : loadFiles(currentParent))}
          className="btn-ghost"
        >
          {loading ? "更新中…" : "重新整理"}
        </button>
        {tab === "tasks" && (
          <label className="flex items-center gap-1 text-xs text-white/60">
            <input
              type="checkbox"
              checked={auto}
              onChange={(e) => setAuto(e.target.checked)}
            />
            自動更新 (8s)
          </label>
        )}
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

      {tab === "tasks" && archiver && (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-xs text-white/70">
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={archiver.enabled}
              onChange={(e) => toggleArchiver(e.target.checked)}
            />
            自動歸檔到 <span className="font-mono">{archiver.archive_folder}/&lt;番號&gt;</span>
          </label>
          <span className="text-white/40">|</span>
          <span>累計 {archiver.archived_total} 個</span>
          {archiver.last_run && (
            <span className="text-white/40">
              最後 {new Date(archiver.last_run + "Z").toLocaleTimeString()}
            </span>
          )}
          <button className="ml-auto text-blue-300 hover:underline" onClick={runArchiverNow}>
            立即執行
          </button>
          {archiver.last_error && (
            <span className="basis-full text-amber-300/80">
              {archiver.last_error}
            </span>
          )}
        </div>
      )}

      {tab === "tasks" && (
        <TasksTable tasks={tasks} onDelete={deleteTasks} onRetry={retryTask} />
      )}

      {tab === "files" && (
        <FilesPanel
          files={files}
          parents={parents}
          search={search}
          onSearch={setSearch}
          onSubmitSearch={runSearch}
          onOpen={openFolder}
          onCrumb={gotoCrumb}
          onTrash={trashFiles}
          onShare={shareFiles}
          onRefresh={() => loadFiles(currentParent)}
        />
      )}
    </div>
  );
}

function TasksTable({
  tasks,
  onDelete,
  onRetry,
}: {
  tasks: PikPakTask[];
  onDelete: (ids: string[]) => void;
  onRetry: (id: string) => void;
}) {
  if (!tasks.length)
    return (
      <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
        沒有離線下載任務
      </div>
    );
  return (
    <div className="overflow-hidden rounded-lg border border-white/10">
      <table className="w-full text-sm">
        <thead className="bg-white/5 text-left text-xs uppercase tracking-wide text-white/40">
          <tr>
            <th className="px-3 py-2">名稱</th>
            <th className="px-3 py-2 w-32">狀態</th>
            <th className="px-3 py-2 w-20">進度</th>
            <th className="px-3 py-2 w-24">大小</th>
            <th className="px-3 py-2 w-32">操作</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => {
            const failed =
              t.phase === "PHASE_TYPE_ERROR" || t.phase === "ERROR";
            const done = t.phase === "PHASE_TYPE_COMPLETE";
            return (
              <tr key={t.id || t.name} className="border-t border-white/5">
                <td className="px-3 py-2">
                  <div className="truncate text-white/90">
                    {t.name || t.id}
                  </div>
                  {t.message && (
                    <div className="text-xs text-white/40">{t.message}</div>
                  )}
                </td>
                <td className="px-3 py-2">
                  <span
                    className={
                      "rounded px-2 py-0.5 text-xs " +
                      (done
                        ? "bg-emerald-400/20 text-emerald-200"
                        : failed
                        ? "bg-red-500/20 text-red-300"
                        : "bg-white/10 text-white/70")
                    }
                  >
                    {t.phase.replace("PHASE_TYPE_", "")}
                  </span>
                </td>
                <td className="px-3 py-2 text-white/70">{t.progress ?? 0}%</td>
                <td className="px-3 py-2 text-white/70">
                  {fmtBytes(t.file_size)}
                </td>
                <td className="px-3 py-2">
                  <div className="flex gap-2 text-xs">
                    {failed && t.id && (
                      <button
                        onClick={() => onRetry(t.id)}
                        className="text-amber-300 hover:underline"
                      >
                        重試
                      </button>
                    )}
                    {t.id && (
                      <button
                        onClick={() => onDelete([t.id])}
                        className="text-red-300 hover:underline"
                      >
                        刪除
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
  );
}

function FilesPanel({
  files,
  parents,
  search,
  onSearch,
  onSubmitSearch,
  onOpen,
  onCrumb,
  onTrash,
  onShare,
  onRefresh,
}: {
  files: PikPakFile[];
  parents: { id: string; name: string }[];
  search: string;
  onSearch: (s: string) => void;
  onSubmitSearch: () => void;
  onOpen: (f: PikPakFile) => void;
  onCrumb: (idx: number) => void;
  onTrash: (ids: string[]) => void;
  onShare: (ids: string[]) => void;
  onRefresh: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());

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
        <div className="ml-auto">
          <CleanupButton
            folder_id={parents[parents.length - 1].id}
            folder_name={parents[parents.length - 1].name}
            disabled={parents.length <= 1}
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

      {selectedIds.length > 0 && (
        <div className="flex gap-2 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
          <span className="text-white/60">已選 {selectedIds.length} 個</span>
          <button
            className="ml-auto text-blue-300 hover:underline"
            onClick={() => onShare(selectedIds)}
          >
            建立分享
          </button>
          <button
            className="text-red-300 hover:underline"
            onClick={() => onTrash(selectedIds)}
          >
            移到垃圾桶
          </button>
        </div>
      )}

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
                <th className="px-3 py-2 w-24">操作</th>
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
                      {f.kind === "drive#folder" ? "📁 " : "📄 "}
                      {f.name}
                    </button>
                  </td>
                  <td className="px-3 py-2 text-white/60">
                    {f.kind === "drive#folder" ? "資料夾" : "檔案"}
                  </td>
                  <td className="px-3 py-2 text-white/70">
                    {fmtBytes(f.size)}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex gap-2 text-xs">
                      <button
                        onClick={() => onShare([f.id])}
                        className="text-blue-300 hover:underline"
                      >
                        分享
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
