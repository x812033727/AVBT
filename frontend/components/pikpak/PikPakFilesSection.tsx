"use client";

import { useMemo, useState } from "react";
import { CloudUpload, FolderInput, Search, Share2, Trash2 } from "lucide-react";
import CleanupButton from "@/components/CleanupButton";
import EpisodeFinderButton from "@/components/EpisodeFinderButton";
import FolderStatsBar from "@/components/FolderStatsBar";
import MoveModal from "@/components/MoveModal";
import PCloudSendModal from "@/components/PCloudSendModal";
import { FilesPanel, type FileRow } from "@/components/files/FilesPanel";
import type { PikPakFile } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// PikPak「雲端檔案」分頁(從 app/pikpak/page.tsx 拆出,props 照原樣):
// 資料載入/導覽 handlers 留在 page 層;這裡持有選取與 modal 開關等
// UI state,列表本體交給通用 <FilesPanel>,平台特有操作走 slot。
export default function PikPakFilesSection({
  files,
  parents,
  currentParent,
  loading,
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
  currentParent: string;
  loading: boolean;
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
  const [moveOpen, setMoveOpen] = useState(false);
  const [pcloudOpen, setPcloudOpen] = useState(false);
  const [pcloudFolder, setPcloudFolder] = useState<
    { id: string; name: string } | null
  >(null);

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
        isFolder: f.kind === "drive#folder",
        size: f.size,
      })),
    [files]
  );

  const selectedIds = Array.from(selected);

  function sendRowToPCloud(row: FileRow) {
    setPcloudFolder(row.isFolder ? { id: row.id, name: row.name } : null);
    if (!row.isFolder) setSelected(new Set([row.id]));
    setPcloudOpen(true);
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <EpisodeFinderButton
          folder_id={parents[parents.length - 1].id}
          folder_name={parents[parents.length - 1].name}
          disabled={parents.length <= 1}
          onDone={onRefresh}
        />
        <CleanupButton
          folder_id={parents[parents.length - 1].id}
          folder_name={parents[parents.length - 1].name}
          disabled={parents.length <= 1}
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

      <FolderStatsBar parentId={currentParent} />

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
              className="inline-flex items-center gap-1 text-emerald-300 hover:underline"
              onClick={() => {
                setPcloudFolder(null);
                setPcloudOpen(true);
              }}
            >
              <CloudUpload className="h-3.5 w-3.5" aria-hidden />
              送 pCloud
            </button>
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
              className="inline-flex items-center gap-1 text-blue-300 hover:underline"
              onClick={() => onShare(selectedIds)}
            >
              <Share2 className="h-3.5 w-3.5" aria-hidden />
              建立分享
            </button>
            <button
              type="button"
              className="inline-flex items-center gap-1 text-red-300 hover:underline"
              onClick={() => onTrash(selectedIds)}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
              移到垃圾桶
            </button>
          </>
        }
        rowActions={(row) => (
          <div className="flex gap-2 text-xs">
            <button
              type="button"
              onClick={() => sendRowToPCloud(row)}
              className="text-emerald-300 hover:underline"
              title={row.isFolder ? "遞迴傳整個資料夾到 pCloud" : "傳此檔到 pCloud"}
            >
              送 pCloud
            </button>
            <button
              type="button"
              onClick={() => onShare([row.id])}
              className="text-blue-300 hover:underline"
            >
              分享
            </button>
            <button
              type="button"
              onClick={() => onTrash([row.id])}
              className="text-red-300 hover:underline"
            >
              刪除
            </button>
          </div>
        )}
      />

      <MoveModal
        open={moveOpen}
        fileIds={selectedIds}
        onClose={() => setMoveOpen(false)}
        onDone={() => {
          setSelected(new Set());
          onRefresh();
        }}
      />

      <PCloudSendModal
        open={pcloudOpen}
        fileIds={pcloudFolder ? undefined : selectedIds}
        folderId={pcloudFolder?.id}
        folderName={pcloudFolder?.name}
        onClose={() => setPcloudOpen(false)}
        onDone={() => {
          if (!pcloudFolder) setSelected(new Set());
        }}
      />
    </div>
  );
}
