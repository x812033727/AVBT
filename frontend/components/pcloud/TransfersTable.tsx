"use client";

import { useMemo } from "react";
import { CloudUpload } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { Progress } from "@/components/ui/progress";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { PCloudTransfer } from "@/lib/api";
import { fmtBytes } from "@/lib/format";
import { transferStatusTone } from "@/lib/status";

// 傳輸任務列表(從 app/pcloud/page.tsx 拆出,props 照原樣):
// 依目的資料夾分組,狀態 pill 統一走 lib/status 的 transferStatusTone。
export default function TransfersTable({
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
      <EmptyState
        icon={CloudUpload}
        title="沒有傳輸任務"
        hint="到 /pikpak 頁勾選檔案後按「送 pCloud」開始"
      />
    );
  }

  return (
    <div className="space-y-3">
      {groups.map(([path, rows]) => (
        <div key={path} className="overflow-hidden rounded-lg border border-border">
          <div className="flex items-center justify-between border-b border-border bg-muted/50 px-3 py-2 text-xs">
            <span className="font-mono text-muted-foreground">{path}</span>
            <span className="text-muted-foreground/60">{rows.length} 個檔案</span>
          </div>
          <div className="overflow-x-auto">
            <Table>
              <TableHeader className="bg-muted/30">
                <TableRow className="hover:bg-transparent">
                  <TableHead className="px-3">檔名</TableHead>
                  <TableHead className="w-24 px-3">狀態</TableHead>
                  <TableHead className="w-40 px-3">進度</TableHead>
                  <TableHead className="w-24 px-3">大小</TableHead>
                  <TableHead className="w-28 px-3">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => {
                  const pct = r.pikpak_size
                    ? Math.min(
                        100,
                        Math.round((r.bytes_downloaded / r.pikpak_size) * 100)
                      )
                    : 0;
                  const view = transferStatusTone(r.status);
                  return (
                    <TableRow key={r.id} className="align-top">
                      <TableCell className="max-w-0 px-3">
                        <div className="truncate text-foreground" title={r.pikpak_name}>
                          {r.pikpak_name || `(file_id ${r.pikpak_file_id})`}
                        </div>
                        {r.pikpak_path && (
                          <div className="text-xs text-muted-foreground/70">
                            來源子路徑: {r.pikpak_path}
                          </div>
                        )}
                        {r.message && (
                          <div className="text-xs text-muted-foreground/70">
                            {r.message}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="px-3">
                        <StatusBadge tone={view.tone}>{view.label}</StatusBadge>
                      </TableCell>
                      <TableCell className="px-3">
                        {r.status === "running" && r.pikpak_size > 0 ? (
                          <div className="min-w-32 space-y-0.5">
                            <Progress value={pct} className="h-1.5" />
                            <div className="text-xs text-muted-foreground">
                              {fmtBytes(r.bytes_downloaded)} /{" "}
                              {fmtBytes(r.pikpak_size)} ({pct}%)
                            </div>
                          </div>
                        ) : r.status === "done" ? (
                          <span className="text-xs text-emerald-300/80">100%</span>
                        ) : (
                          <span className="text-xs text-muted-foreground/60">—</span>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-nowrap px-3 text-muted-foreground">
                        {fmtBytes(r.pikpak_size)}
                      </TableCell>
                      <TableCell className="px-3">
                        <div className="flex gap-2 text-xs">
                          {(r.status === "failed" || r.status === "cancelled") && (
                            <button
                              type="button"
                              onClick={() => onRetry(r.id)}
                              className="text-amber-300 hover:underline"
                            >
                              重試
                            </button>
                          )}
                          {(r.status === "pending" || r.status === "running") && (
                            <button
                              type="button"
                              onClick={() => onCancel(r.id)}
                              className="text-red-300 hover:underline"
                            >
                              取消
                            </button>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        </div>
      ))}
    </div>
  );
}
