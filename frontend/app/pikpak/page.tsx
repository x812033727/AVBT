"use client";

import { useEffect, useState } from "react";
import {
  api,
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

export default function PikpakPage() {
  const [tab, setTab] = useState<"tasks" | "files">("tasks");
  const [quota, setQuota] = useState<PikPakQuota | null>(null);
  const [tasks, setTasks] = useState<PikPakTask[]>([]);
  const [files, setFiles] = useState<PikPakFile[]>([]);
  const [parents, setParents] = useState<{ id: string; name: string }[]>([
    { id: "", name: "我的雲盤" },
  ]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function loadAll() {
    setError(null);
    setLoading(true);
    try {
      const [q, t] = await Promise.all([
        api.get<PikPakQuota>("/api/pikpak/quota").catch(() => null),
        api.get<PikPakTask[]>("/api/pikpak/tasks"),
      ]);
      setQuota(q);
      setTasks(t);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadFiles(parentId: string) {
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
  }

  useEffect(() => {
    loadAll();
  }, []);

  useEffect(() => {
    if (tab === "files") loadFiles(parents[parents.length - 1].id);
  }, [tab, parents]);

  async function openFolder(f: PikPakFile) {
    if (f.kind !== "drive#folder") {
      const { url } = await api.get<{ url: string }>(
        `/api/pikpak/files/${f.id}/url`
      );
      if (url) window.open(url, "_blank");
      return;
    }
    setParents([...parents, { id: f.id, name: f.name }]);
  }

  function gotoCrumb(idx: number) {
    setParents(parents.slice(0, idx + 1));
  }

  async function deleteTasks(ids: string[]) {
    if (!ids.length) return;
    if (!confirm(`刪除 ${ids.length} 個任務？`)) return;
    await api.post("/api/pikpak/tasks/delete", { task_ids: ids, delete_files: false });
    loadAll();
  }

  async function trashFiles(ids: string[]) {
    if (!ids.length) return;
    if (!confirm(`移到垃圾桶 ${ids.length} 個檔案？`)) return;
    await api.post("/api/pikpak/files/trash", { ids });
    loadFiles(parents[parents.length - 1].id);
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
        <button onClick={() => (tab === "tasks" ? loadAll() : loadFiles(parents[parents.length - 1].id))} className="btn-ghost">
          {loading ? "更新中…" : "重新整理"}
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

      {tab === "tasks" && <TasksTable tasks={tasks} onDelete={deleteTasks} />}

      {tab === "files" && (
        <FilesPanel
          files={files}
          parents={parents}
          onOpen={openFolder}
          onCrumb={gotoCrumb}
          onTrash={trashFiles}
        />
      )}
    </div>
  );
}

function TasksTable({
  tasks,
  onDelete,
}: {
  tasks: PikPakTask[];
  onDelete: (ids: string[]) => void;
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
            <th className="px-3 py-2 w-24">狀態</th>
            <th className="px-3 py-2 w-20">進度</th>
            <th className="px-3 py-2 w-24">大小</th>
            <th className="px-3 py-2 w-20">操作</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => (
            <tr key={t.id} className="border-t border-white/5">
              <td className="px-3 py-2">
                <div className="truncate text-white/90">{t.name || t.id}</div>
                {t.message && (
                  <div className="text-xs text-white/40">{t.message}</div>
                )}
              </td>
              <td className="px-3 py-2 text-white/70">{t.phase}</td>
              <td className="px-3 py-2 text-white/70">{t.progress ?? 0}%</td>
              <td className="px-3 py-2 text-white/70">{fmtBytes(t.file_size)}</td>
              <td className="px-3 py-2">
                <button
                  onClick={() => onDelete([t.id])}
                  className="text-red-300 hover:underline"
                >
                  刪除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FilesPanel({
  files,
  parents,
  onOpen,
  onCrumb,
  onTrash,
}: {
  files: PikPakFile[];
  parents: { id: string; name: string }[];
  onOpen: (f: PikPakFile) => void;
  onCrumb: (idx: number) => void;
  onTrash: (ids: string[]) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1 text-sm text-white/60">
        {parents.map((p, i) => (
          <span key={p.id + i} className="flex items-center gap-1">
            {i > 0 && <span className="text-white/30">/</span>}
            <button
              className="hover:text-accent"
              onClick={() => onCrumb(i)}
            >
              {p.name}
            </button>
          </span>
        ))}
      </div>
      {!files.length ? (
        <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
          此資料夾為空
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-white/10">
          <table className="w-full text-sm">
            <thead className="bg-white/5 text-left text-xs uppercase tracking-wide text-white/40">
              <tr>
                <th className="px-3 py-2">名稱</th>
                <th className="px-3 py-2 w-24">類型</th>
                <th className="px-3 py-2 w-28">大小</th>
                <th className="px-3 py-2 w-20">操作</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.id} className="border-t border-white/5">
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
                    <button
                      onClick={() => onTrash([f.id])}
                      className="text-red-300 hover:underline"
                    >
                      刪除
                    </button>
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
