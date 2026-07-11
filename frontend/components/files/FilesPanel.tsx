"use client";

import type { ReactNode } from "react";
import { File, Folder, Play } from "lucide-react";
import { fmtBytes, fmtDateTime } from "@/lib/format";
import { isVideo } from "@/lib/video";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/shared/EmptyState";

// 通用雲端檔案列表(PikPak / pCloud 共用):
// - 受控元件:選取 state 由呼叫端持有,這裡只發 toggle 事件。
// - 平台特有操作一律走 slot:批次列用 toolbar、每列右側用 rowActions。
// - 「修改時間」欄只有在任一列帶 modified 時才會出現(pCloud 有、PikPak 沒有)。

export type FileRow = {
  id: string;
  name: string;
  isFolder: boolean;
  size?: number | null;
  modified?: string | null;
};

export type Crumb = { id: string; name: string };

function RowIcon({ row }: { row: FileRow }) {
  const cls = "h-4 w-4 shrink-0 text-muted-foreground";
  if (row.isFolder) return <Folder className={cls} aria-hidden />;
  if (isVideo(row.name)) return <Play className={cls} aria-hidden />;
  return <File className={cls} aria-hidden />;
}

export function FilesPanel({
  rows,
  crumbs,
  loading,
  selected,
  onToggleSelect,
  onToggleSelectAll,
  onOpen,
  onCrumbClick,
  toolbar,
  rowActions,
  emptyText = "此資料夾為空",
}: {
  rows: FileRow[];
  crumbs: Crumb[];
  loading: boolean;
  selected: Set<string>;
  onToggleSelect: (id: string) => void;
  onToggleSelectAll: () => void;
  onOpen: (row: FileRow) => void;
  onCrumbClick: (idx: number) => void;
  /** 批次操作列 slot:選取數 > 0 時顯示在「已選 N 個」右側。 */
  toolbar?: ReactNode;
  /** 每列右側操作 slot(播放/分享/改名等平台特有)。 */
  rowActions?: (row: FileRow) => ReactNode;
  emptyText?: string;
}) {
  const showModified = rows.some((r) => r.modified !== undefined);
  const allSelected = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const someSelected = rows.some((r) => selected.has(r.id));
  const columns = 3 + (showModified ? 1 : 0) + (rowActions ? 1 : 0);

  return (
    <div className="space-y-3">
      <nav
        aria-label="路徑"
        className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground"
      >
        {crumbs.map((c, i) => (
          <span key={c.id + i} className="flex items-center gap-1">
            {i > 0 && <span className="text-muted-foreground/40">/</span>}
            <button
              type="button"
              className={
                i === crumbs.length - 1
                  ? "text-foreground"
                  : "transition-colors hover:text-primary"
              }
              onClick={() => onCrumbClick(i)}
            >
              {c.name}
            </button>
          </span>
        ))}
      </nav>

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-sm">
          <span className="text-muted-foreground">已選 {selected.size} 個</span>
          {toolbar ? <div className="ml-auto flex flex-wrap items-center gap-2">{toolbar}</div> : null}
        </div>
      )}

      {!loading && !rows.length ? (
        <EmptyState icon={Folder} title={emptyText} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <Table>
            <TableHeader className="bg-muted/50">
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-10 px-3">
                  <Checkbox
                    aria-label="全選"
                    checked={allSelected ? true : someSelected ? "indeterminate" : false}
                    onCheckedChange={onToggleSelectAll}
                    disabled={loading || !rows.length}
                  />
                </TableHead>
                <TableHead className="px-3">名稱</TableHead>
                <TableHead className="w-28 px-3">大小</TableHead>
                {showModified && <TableHead className="w-40 px-3">修改時間</TableHead>}
                {rowActions && <TableHead className="w-32 px-3">操作</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading
                ? Array.from({ length: 6 }).map((_, i) => (
                    <TableRow key={`skeleton-${i}`} className="hover:bg-transparent">
                      {Array.from({ length: columns }).map((_, j) => (
                        <TableCell key={j} className="px-3 py-2.5">
                          <Skeleton className={j === 1 ? "h-4 w-3/4" : "h-4 w-full max-w-16"} />
                        </TableCell>
                      ))}
                    </TableRow>
                  ))
                : rows.map((row) => (
                    <TableRow key={row.id} data-state={selected.has(row.id) ? "selected" : undefined}>
                      <TableCell className="px-3">
                        <Checkbox
                          aria-label={`選取 ${row.name}`}
                          checked={selected.has(row.id)}
                          onCheckedChange={() => onToggleSelect(row.id)}
                        />
                      </TableCell>
                      <TableCell className="max-w-0 px-3">
                        <button
                          type="button"
                          className="flex w-full min-w-48 items-center gap-2 text-left text-foreground transition-colors hover:text-primary"
                          onClick={() => onOpen(row)}
                          title={row.name}
                        >
                          <RowIcon row={row} />
                          <span className="truncate">{row.name}</span>
                        </button>
                      </TableCell>
                      <TableCell className="whitespace-nowrap px-3 text-muted-foreground">
                        {row.isFolder ? "-" : fmtBytes(row.size)}
                      </TableCell>
                      {showModified && (
                        <TableCell className="whitespace-nowrap px-3 text-muted-foreground">
                          {fmtDateTime(row.modified)}
                        </TableCell>
                      )}
                      {rowActions && (
                        <TableCell className="whitespace-nowrap px-3">{rowActions(row)}</TableCell>
                      )}
                    </TableRow>
                  ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
