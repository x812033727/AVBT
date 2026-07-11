"use client";

import { Button } from "@/components/ui/button";
import type { PCloudQueueStatus } from "@/lib/api";

// 傳輸佇列統計列(從 app/pcloud/page.tsx 拆出,props 照原樣)。
export default function QueueBar({
  queue,
  onCleanup,
}: {
  queue: PCloudQueueStatus | null;
  onCleanup: (keepFailed: boolean) => void;
}) {
  if (!queue) return null;
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground">
      <span>排隊 {queue.pending}</span>
      <span className="text-muted-foreground/40">|</span>
      <span>傳輸中 {queue.running}</span>
      <span className="text-muted-foreground/40">|</span>
      <span className="text-emerald-300/80">完成 {queue.done}</span>
      <span className="text-muted-foreground/40">|</span>
      <span className="text-red-300/80">失敗 {queue.failed}</span>
      <span className="text-muted-foreground/40">|</span>
      <span className="text-muted-foreground/70">
        併發上限 {queue.concurrency}・本機已送出 {queue.inflight}
      </span>
      <Button
        variant="outline"
        size="sm"
        className="ml-auto h-6 px-2"
        onClick={() => onCleanup(true)}
        title="清掉 已完成 + 已取消"
      >
        清掉完成
      </Button>
      <Button
        variant="outline"
        size="sm"
        className="h-6 border-red-500/30 px-2 text-red-300 hover:bg-red-500/10 hover:text-red-300"
        onClick={() => onCleanup(false)}
        title="清掉 已完成 + 已取消 + 失敗"
      >
        清掉所有結束項
      </Button>
    </div>
  );
}
