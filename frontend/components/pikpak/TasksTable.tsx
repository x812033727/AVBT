"use client";

import { Inbox } from "lucide-react";
import type { PikPakTask } from "@/lib/api";
import { fmtBytes } from "@/lib/format";
import { pikpakPhaseTone } from "@/lib/status";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { EmptyState } from "@/components/shared/EmptyState";
import { Progress } from "@/components/ui/progress";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// PikPak 離線下載任務表(從 app/pikpak/page.tsx 拆出,props 照原樣)。
export default function TasksTable({
  tasks,
  onDelete,
  onRetry,
}: {
  tasks: PikPakTask[];
  onDelete: (ids: string[]) => void;
  onRetry: (id: string) => void;
}) {
  if (!tasks.length) return <EmptyState icon={Inbox} title="沒有離線下載任務" />;

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <Table>
        <TableHeader className="bg-muted/50">
          <TableRow className="hover:bg-transparent">
            <TableHead className="px-3">名稱</TableHead>
            <TableHead className="w-28 px-3">狀態</TableHead>
            <TableHead className="w-32 px-3">進度</TableHead>
            <TableHead className="w-28 px-3">大小</TableHead>
            <TableHead className="w-32 px-3">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {tasks.map((t) => {
            const failed = t.phase === "PHASE_TYPE_ERROR" || t.phase === "ERROR";
            const view = pikpakPhaseTone(t.phase);
            const pct = t.progress ?? 0;
            return (
              <TableRow key={t.id || t.name}>
                <TableCell className="max-w-0 px-3">
                  <div className="truncate text-foreground" title={t.name || t.id}>
                    {t.name || t.id}
                  </div>
                  {t.message && (
                    <div className="truncate text-xs text-muted-foreground">{t.message}</div>
                  )}
                </TableCell>
                <TableCell className="px-3">
                  <StatusBadge tone={view.tone}>{view.label}</StatusBadge>
                </TableCell>
                <TableCell className="px-3">
                  <div className="flex items-center gap-2">
                    <Progress value={pct} className="h-1.5 w-16" />
                    <span className="text-xs tabular-nums text-muted-foreground">{pct}%</span>
                  </div>
                </TableCell>
                <TableCell className="whitespace-nowrap px-3 text-muted-foreground">
                  {fmtBytes(t.file_size)}
                </TableCell>
                <TableCell className="px-3">
                  <div className="flex gap-2 text-xs">
                    {failed && t.id && (
                      <button
                        type="button"
                        onClick={() => onRetry(t.id)}
                        className="text-amber-300 hover:underline"
                      >
                        重試
                      </button>
                    )}
                    {t.id && (
                      <button
                        type="button"
                        onClick={() => onDelete([t.id])}
                        className="text-red-300 hover:underline"
                      >
                        刪除
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
  );
}
