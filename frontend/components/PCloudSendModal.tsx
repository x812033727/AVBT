"use client";

import { useEffect, useState } from "react";
import {
  api,
  type PCloudEnqueueResult,
  type PCloudFile,
  type PCloudStatus,
} from "@/lib/api";
import { toast } from "@/components/Toast";

type Crumb = { id: string; name: string };

/**
 * Send selected PikPak file(s) or one PikPak folder to pCloud.
 *
 * - ``fileIds`` set:每個檔案分別送到同一 pCloud 目錄
 * - ``folderId`` set:遞迴整個資料夾,可選保留子目錄結構
 *
 * 兩個都不傳 modal 不會開,擇一非空即可。
 */
export default function PCloudSendModal({
  open,
  fileIds,
  folderId,
  folderName,
  onClose,
  onDone,
}: {
  open: boolean;
  fileIds?: string[];
  folderId?: string;
  folderName?: string;
  onClose: () => void;
  onDone?: (res: PCloudEnqueueResult) => void;
}) {
  const [status, setStatus] = useState<PCloudStatus | null>(null);
  const [crumbs, setCrumbs] = useState<Crumb[]>([{ id: "0", name: "我的 pCloud" }]);
  const [entries, setEntries] = useState<PCloudFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [pathOverride, setPathOverride] = useState("");
  const [deleteSource, setDeleteSource] = useState(false);
  const [preserveSubfolders, setPreserveSubfolders] = useState(true);
  const [newFolderName, setNewFolderName] = useState("");
  const [creating, setCreating] = useState(false);

  const currentId = crumbs[crumbs.length - 1].id;
  const mode = folderId ? "folder" : "files";
  const fileCount = fileIds?.length ?? 0;

  useEffect(() => {
    if (!open) return;
    let alive = true;
    api
      .get<PCloudStatus>("/api/pcloud/status")
      .then((s) => {
        if (!alive) return;
        setStatus(s);
        if (s.default_folder) setPathOverride(s.default_folder);
      })
      .catch((e: any) => alive && toast.error(e.message));
    return () => {
      alive = false;
    };
  }, [open]);

  useEffect(() => {
    if (!open || !status?.logged_in) {
      if (!open) {
        setCrumbs([{ id: "0", name: "我的 pCloud" }]);
        setEntries([]);
        setNewFolderName("");
      }
      return;
    }
    let alive = true;
    setLoading(true);
    api
      .get<PCloudFile[]>(
        `/api/pcloud/files?parent_id=${encodeURIComponent(currentId)}`
      )
      .then((res) => alive && setEntries(res))
      .catch((e: any) => alive && toast.error(e.message || "讀取 pCloud 資料夾失敗"))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [open, currentId, status?.logged_in]);

  function openFolder(entry: PCloudFile) {
    if (entry.kind !== "folder") return;
    setCrumbs([...crumbs, { id: entry.id, name: entry.name }]);
    setPathOverride("");
  }

  function gotoCrumb(idx: number) {
    setCrumbs(crumbs.slice(0, idx + 1));
    setPathOverride("");
  }

  // Walk the breadcrumb chain to build the current absolute pCloud path.
  function currentPath(): string {
    if (crumbs.length <= 1) return "/";
    return "/" + crumbs.slice(1).map((c) => c.name).join("/");
  }

  async function createFolder() {
    const name = newFolderName.trim();
    if (!name) return;
    setCreating(true);
    try {
      await api.post("/api/pcloud/folders/create", {
        parent_id: currentId,
        name,
      });
      toast.success(`已建立 ${name}`);
      setNewFolderName("");
      const res = await api.get<PCloudFile[]>(
        `/api/pcloud/files?parent_id=${encodeURIComponent(currentId)}`
      );
      setEntries(res);
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setCreating(false);
    }
  }

  function effectivePath(): string {
    const override = pathOverride.trim();
    if (override) return override.startsWith("/") ? override : "/" + override;
    return currentPath();
  }

  async function submit() {
    if (submitting) return;
    if (mode === "files" && !fileCount) return;
    if (mode === "folder" && !folderId) return;
    setSubmitting(true);
    try {
      const res = await api.post<PCloudEnqueueResult>("/api/pcloud/transfers", {
        pikpak_file_ids: mode === "files" ? fileIds : [],
        pikpak_folder_id: mode === "folder" ? folderId : "",
        folder: effectivePath(),
        delete_source: deleteSource,
        preserve_subfolders: preserveSubfolders,
      });
      toast.success(`已排入 ${res.enqueued} 個檔案 → ${res.folder_path}`);
      onDone?.(res);
      onClose();
    } catch (e: any) {
      toast.error(e.message || "送出失敗");
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) return null;

  const folderEntries = entries.filter((e) => e.kind === "folder");

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl rounded-lg border border-white/10 bg-panel shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <div className="text-sm font-medium">
            {mode === "folder"
              ? `送整個資料夾「${folderName || folderId}」到 pCloud`
              : `送 ${fileCount} 個檔案到 pCloud`}
          </div>
          <button onClick={onClose} className="text-white/60 hover:text-white">
            ✕
          </button>
        </div>

        {!status?.logged_in ? (
          <div className="space-y-3 p-6 text-center">
            <div className="text-sm text-white/70">尚未登入 pCloud。</div>
            <a
              href="/pcloud"
              className="inline-block rounded-md border border-accent/40 bg-accent/10 px-3 py-1.5 text-sm text-accent hover:bg-accent/20"
            >
              前往 /pcloud 登入
            </a>
          </div>
        ) : (
          <div className="space-y-3 p-4">
            <div className="flex flex-wrap items-center gap-1 text-sm text-white/60">
              {crumbs.map((c, i) => (
                <span key={`${c.id}-${i}`} className="flex items-center gap-1">
                  {i > 0 && <span className="text-white/30">/</span>}
                  <button className="hover:text-accent" onClick={() => gotoCrumb(i)}>
                    {c.name}
                  </button>
                </span>
              ))}
            </div>

            <div className="max-h-[34vh] min-h-[10rem] overflow-auto rounded-md border border-white/10">
              {loading ? (
                <div className="px-3 py-6 text-center text-sm text-white/40">
                  載入中…
                </div>
              ) : !folderEntries.length ? (
                <div className="px-3 py-6 text-center text-sm text-white/40">
                  此目錄沒有子資料夾
                </div>
              ) : (
                <ul className="divide-y divide-white/5 text-sm">
                  {folderEntries.map((f) => (
                    <li key={`f-${f.id}`}>
                      <button
                        onClick={() => openFolder(f)}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-white/5"
                      >
                        <span>📁</span>
                        <span className="truncate text-white/90">{f.name}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="flex gap-1">
              <input
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                placeholder="在此目錄下建立新資料夾…"
                className="flex-1 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
              />
              <button
                onClick={createFolder}
                className="btn-ghost"
                disabled={creating || !newFolderName.trim()}
              >
                {creating ? "建立中…" : "建立"}
              </button>
            </div>

            <div className="space-y-1">
              <label className="block text-xs text-white/60">
                目標路徑(可直接輸入,會自動建立)
              </label>
              <input
                value={pathOverride}
                onChange={(e) => setPathOverride(e.target.value)}
                placeholder={currentPath()}
                className="w-full rounded-md border border-white/10 bg-panel px-2 py-1 font-mono text-sm outline-none focus:border-accent"
              />
            </div>

            <div className="flex flex-wrap gap-3 text-xs text-white/70">
              {mode === "folder" && (
                <label className="flex items-center gap-1">
                  <input
                    type="checkbox"
                    checked={preserveSubfolders}
                    onChange={(e) => setPreserveSubfolders(e.target.checked)}
                  />
                  保留子資料夾結構
                </label>
              )}
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={deleteSource}
                  onChange={(e) => setDeleteSource(e.target.checked)}
                />
                傳輸成功後將 PikPak 原檔移到垃圾桶
              </label>
            </div>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-white/10 px-4 py-3">
          <button className="btn-ghost" onClick={onClose}>
            取消
          </button>
          <button
            className="btn-primary"
            onClick={submit}
            disabled={
              !status?.logged_in ||
              submitting ||
              (mode === "files" ? !fileCount : !folderId)
            }
          >
            {submitting ? "排入中…" : "開始傳輸"}
          </button>
        </div>
      </div>
    </div>
  );
}
