"use client";

import { useCallback, useEffect, useState } from "react";
import PCloudCleanupButton from "@/components/PCloudCleanupButton";
import PCloudMoveModal from "@/components/PCloudMoveModal";
import { confirmDialog, toast } from "@/components/Toast";
import {
  api,
  type PCloudFile,
  type PCloudFolderStats,
  type PCloudQuota,
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

export default function PCloudPage() {
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
    loadFiles(currentParent);
  }, [currentParent, loadFiles]);

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
    const ok = await confirmDialog(`刪除 ${ids.length} 個項目？`, "資料夾將連同內容一起刪除");
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
        `/api/pcloud/files/search?q=${encodeURIComponent(search.trim())}&parent_id=${encodeURIComponent(currentParent)}`
      );
      setFiles(res);
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="text-sm font-medium text-white/80">pCloud 雲端檔案</div>
        <button
          onClick={() => loadFiles(currentParent)}
          className="btn-ghost"
        >
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

      <PCloudFolderStats parentId={currentParent} />

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
                  <td className="px-3 py-2 text-white/70">
                    {fmtBytes(f.size)}
                  </td>
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

function PCloudFolderStats({ parentId }: { parentId: string }) {
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
        <span className="ml-1 text-white/40">資料夾</span>{" "}
        {stats.total_folders}
      </span>
      <span>
        <span className="text-white/40">總大小</span>{" "}
        {fmtBytes(stats.total_size)}
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
      {stats.partial && (
        <span className="text-amber-300/80">(部分統計)</span>
      )}
    </div>
  );
}
