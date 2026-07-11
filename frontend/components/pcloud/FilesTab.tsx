"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  FolderInput,
  FolderPlus,
  PenLine,
  RotateCw,
  Search,
  Trash2,
} from "lucide-react";
import EpisodeFinderButton from "@/components/EpisodeFinderButton";
import PCloudCleanupButton from "@/components/PCloudCleanupButton";
import PCloudMoveModal from "@/components/PCloudMoveModal";
import PCloudOrganizeButton from "@/components/PCloudOrganizeButton";
import { confirmDialog, toast } from "@/components/Toast";
import { FilesPanel, type FileRow } from "@/components/files/FilesPanel";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  api,
  type PCloudFile,
  type PCloudFolderStats,
  type PCloudQuota,
} from "@/lib/api";
import { fmtBytes } from "@/lib/format";

// pCloud「雲端檔案」分頁(從 app/pcloud/page.tsx 拆出,props 照原樣):
// 資料載入/導覽 handlers 在 FilesTab;選取與移動 modal 等 UI state 在
// PCloudFilesSection,列表本體交給通用 <FilesPanel>,平台特有操作走 slot。
export default function FilesTab({ loggedIn }: { loggedIn: boolean }) {
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
        <Button
          variant="ghost"
          size="sm"
          onClick={() => loadFiles(currentParent)}
        >
          <RotateCw aria-hidden />
          {loading ? "更新中…" : "重新整理"}
        </Button>
        <Button variant="ghost" size="sm" onClick={createFolder}>
          <FolderPlus aria-hidden />
          新增資料夾
        </Button>
        {quota && (
          <div className="ml-auto text-xs text-muted-foreground">
            已用 {fmtBytes(quota.used)} / {fmtBytes(quota.limit)}
          </div>
        )}
      </div>

      {error && <ErrorBox message={error} />}

      <PCloudFilesSection
        files={files}
        parents={parents}
        currentParent={currentParent}
        currentName={currentName}
        atRoot={atRoot}
        loading={loading}
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

function PCloudFilesSection({
  files,
  parents,
  currentParent,
  currentName,
  atRoot,
  loading,
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
  loading: boolean;
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

  function toggleAll() {
    if (files.length && selected.size === files.length) setSelected(new Set());
    else setSelected(new Set(files.map((f) => f.id)));
  }

  const byId = useMemo(() => new Map(files.map((f) => [f.id, f])), [files]);
  const rows: FileRow[] = useMemo(
    () =>
      files.map((f) => ({
        id: f.id,
        name: f.name,
        isFolder: f.kind === "folder",
        size: f.size,
      })),
    [files]
  );

  const selectedIds = Array.from(selected);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <EpisodeFinderButton
          apiBase="/api/pcloud"
          folder_id={currentParent}
          folder_name={currentName}
          disabled={atRoot}
          onDone={onRefresh}
        />
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
        <form
          className="ml-auto flex gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            onSubmitSearch();
          }}
        >
          <Input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="搜尋此目錄的檔案"
            className="h-8 w-56"
          />
          <Button type="submit" variant="ghost" size="sm">
            <Search aria-hidden />
            搜尋
          </Button>
        </form>
      </div>

      <PCloudFolderStatsBar parentId={currentParent} />

      <FilesPanel
        rows={rows}
        crumbs={parents}
        loading={loading}
        selected={selected}
        onToggleSelect={toggle}
        onToggleSelectAll={toggleAll}
        onOpen={(row) => {
          const f = byId.get(row.id);
          if (f) onOpen(f);
        }}
        onCrumbClick={onCrumb}
        emptyText="此資料夾為空"
        toolbar={
          <>
            <button
              type="button"
              className="inline-flex items-center gap-1 text-amber-300 hover:underline"
              onClick={() => setMoveOpen(true)}
            >
              <FolderInput className="h-3.5 w-3.5" aria-hidden />
              移動到…
            </button>
            <button
              type="button"
              className="inline-flex items-center gap-1 text-red-300 hover:underline"
              onClick={() => onTrash(selectedIds)}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
              刪除
            </button>
          </>
        }
        rowActions={(row) => (
          <div className="flex gap-2 text-xs">
            <button
              type="button"
              onClick={() => {
                const f = byId.get(row.id);
                if (f) onRename(f);
              }}
              className="inline-flex items-center gap-1 text-cyan-300 hover:underline"
            >
              <PenLine className="h-3 w-3" aria-hidden />
              改名
            </button>
            <button
              type="button"
              onClick={() => onTrash([row.id])}
              className="inline-flex items-center gap-1 text-red-300 hover:underline"
            >
              <Trash2 className="h-3 w-3" aria-hidden />
              刪除
            </button>
          </div>
        )}
      />

      <PCloudMoveModal
        open={moveOpen}
        fileIds={selectedIds}
        onClose={() => setMoveOpen(false)}
        onDone={() => {
          setSelected(new Set());
          onRefresh();
        }}
      />
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
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border/50 bg-card/50 px-3 py-1.5 text-xs text-muted-foreground">
      <span>
        <span className="text-muted-foreground/60">檔案</span> {stats.total_files} ·
        <span className="ml-1 text-muted-foreground/60">資料夾</span>{" "}
        {stats.total_folders}
      </span>
      <span>
        <span className="text-muted-foreground/60">總大小</span>{" "}
        {fmtBytes(stats.total_size)}
      </span>
      {stats.video_count > 0 && (
        <span>
          <span className="text-muted-foreground/60">影片</span> {stats.video_count} (
          {fmtBytes(stats.video_size)})
        </span>
      )}
      {stats.coded_count > 0 && (
        <span>
          <span className="text-muted-foreground/60">有番號</span> {stats.coded_count}
        </span>
      )}
      {stats.partial && <span className="text-amber-300/80">(部分統計)</span>}
    </div>
  );
}
