"use client";

import { useEffect, useState } from "react";
import { Folder } from "lucide-react";
import {
  api,
  type PCloudEnqueueResult,
  type PCloudFile,
  type PCloudStatus,
} from "@/lib/api";
import { toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

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

  const folderEntries = entries.filter((e) => e.kind === "folder");

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-base">
            {mode === "folder"
              ? `送整個資料夾「${folderName || folderId}」到 pCloud`
              : `送 ${fileCount} 個檔案到 pCloud`}
          </DialogTitle>
        </DialogHeader>

        {!status?.logged_in ? (
          <div className="space-y-3 py-4 text-center">
            <div className="text-sm text-muted-foreground">尚未登入 pCloud。</div>
            <Button asChild variant="outline">
              <a href="/pcloud">前往 /pcloud 登入</a>
            </Button>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground">
              {crumbs.map((c, i) => (
                <span key={`${c.id}-${i}`} className="flex items-center gap-1">
                  {i > 0 && <span className="text-muted-foreground/40">/</span>}
                  <button
                    type="button"
                    className="transition-colors hover:text-primary"
                    onClick={() => gotoCrumb(i)}
                  >
                    {c.name}
                  </button>
                </span>
              ))}
            </div>

            <div className="max-h-[34vh] min-h-[10rem] overflow-auto rounded-md border border-border">
              {loading ? (
                <div className="px-3 py-6 text-center text-sm text-muted-foreground/70">
                  載入中…
                </div>
              ) : !folderEntries.length ? (
                <div className="px-3 py-6 text-center text-sm text-muted-foreground/70">
                  此目錄沒有子資料夾
                </div>
              ) : (
                <ul className="divide-y divide-border/50 text-sm">
                  {folderEntries.map((f) => (
                    <li key={`f-${f.id}`}>
                      <button
                        type="button"
                        onClick={() => openFolder(f)}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-muted"
                      >
                        <Folder className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                        <span className="truncate text-foreground">{f.name}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="flex gap-1">
              <Input
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                placeholder="在此目錄下建立新資料夾…"
                className="h-8 flex-1"
              />
              <Button
                variant="ghost"
                size="sm"
                onClick={createFolder}
                disabled={creating || !newFolderName.trim()}
              >
                {creating ? "建立中…" : "建立"}
              </Button>
            </div>

            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground">
                目標路徑(可直接輸入,會自動建立)
              </label>
              <Input
                value={pathOverride}
                onChange={(e) => setPathOverride(e.target.value)}
                placeholder={currentPath()}
                className="h-8 font-mono"
              />
            </div>

            <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
              {mode === "folder" && (
                <label className="flex items-center gap-1.5">
                  <Checkbox
                    checked={preserveSubfolders}
                    onCheckedChange={(v) => setPreserveSubfolders(v === true)}
                  />
                  保留子資料夾結構
                </label>
              )}
              <label className="flex items-center gap-1.5">
                <Checkbox
                  checked={deleteSource}
                  onCheckedChange={(v) => setDeleteSource(v === true)}
                />
                傳輸成功後將 PikPak 原檔移到垃圾桶
              </label>
            </div>
          </div>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button
            onClick={submit}
            disabled={
              !status?.logged_in ||
              submitting ||
              (mode === "files" ? !fileCount : !folderId)
            }
          >
            {submitting ? "排入中…" : "開始傳輸"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
