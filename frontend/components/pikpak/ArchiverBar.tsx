"use client";

import Link from "next/link";
import { TriangleAlert } from "lucide-react";
import LegacySweepButton from "@/components/LegacySweepButton";
import type { ArchiverStatus } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";

// 自動歸檔控制列(從 app/pikpak/page.tsx 拆出):純呈現,
// 狀態與 API handlers 仍留在 page 層。
export default function ArchiverBar({
  archiver,
  onToggle,
  onSweep,
  onRunNow,
  onReload,
}: {
  archiver: ArchiverStatus;
  onToggle: (enabled: boolean) => void;
  onSweep: () => void;
  onRunNow: () => void;
  onReload: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground">
      <label className="flex items-center gap-2">
        <Checkbox
          checked={archiver.enabled}
          onCheckedChange={(v) => onToggle(v === true)}
        />
        <span>
          自動歸檔到 <span className="font-mono">{archiver.archive_folder}/&lt;番號&gt;</span>
        </span>
      </label>
      <span className="text-muted-foreground/40">|</span>
      <span>累計 {archiver.archived_total} 個</span>
      {archiver.last_run && (
        <span className="text-muted-foreground/60">最後 {fmtTime(archiver.last_run)}</span>
      )}
      <span className="font-mono text-muted-foreground/60">
        finalize×{archiver.finalize_concurrency}
      </span>
      <span className="font-mono text-muted-foreground/60">
        poll×{archiver.pcloud_poll_concurrency}
      </span>
      {!!archiver.abandoned_total && (
        <Link
          href="/history?abandoned=true"
          className="rounded bg-amber-500/20 px-1.5 py-0.5 font-mono text-amber-300 hover:bg-amber-500/30"
        >
          放棄 {archiver.abandoned_total}
        </Link>
      )}
      <Button
        variant="outline"
        size="sm"
        className="ml-auto h-6 px-2 text-xs"
        onClick={onSweep}
        title={`掃描 ${archiver.task_folder}/ 把已下載完的搬到對應的 系列/女優/... 資料夾`}
      >
        掃描 TASK 並搬移
      </Button>
      <LegacySweepButton archiveFolder={archiver.archive_folder} onDone={onReload} />
      <Button
        variant="ghost"
        size="sm"
        className="h-6 px-2 text-xs text-primary hover:text-primary"
        onClick={onRunNow}
      >
        立即歸檔
      </Button>
      {archiver.last_error && (
        <span className="flex basis-full items-center gap-1 text-amber-300/80">
          <TriangleAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {archiver.last_error}
        </span>
      )}
    </div>
  );
}
