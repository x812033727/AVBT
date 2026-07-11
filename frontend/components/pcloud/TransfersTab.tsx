"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { RotateCw } from "lucide-react";
import QueueBar from "@/components/pcloud/QueueBar";
import TransfersTable from "@/components/pcloud/TransfersTable";
import { confirmDialog, toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  api,
  type PCloudQueueStatus,
  type PCloudTransferPage,
} from "@/lib/api";

const STATUS_FILTERS = [
  { key: "", label: "全部" },
  { key: "pending", label: "等待中" },
  { key: "running", label: "傳輸中" },
  { key: "done", label: "完成" },
  { key: "failed", label: "失敗" },
  { key: "cancelled", label: "已取消" },
] as const;

// pCloud「PikPak 傳輸佇列」分頁(從 app/pcloud/page.tsx 拆出):
// 佇列/清單載入與 5s 輪詢邏輯原樣搬家,只換 UI 外皮。
export default function TransfersTab({ loggedIn }: { loggedIn: boolean }) {
  const [queue, setQueue] = useState<PCloudQueueStatus | null>(null);
  const [page, setPage] = useState<PCloudTransferPage | null>(null);
  const [filter, setFilter] = useState<string>("");
  const [auto, setAuto] = useState(true);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [q, p] = await Promise.all([
        api.get<PCloudQueueStatus>("/api/pcloud/queue"),
        api.get<PCloudTransferPage>(
          `/api/pcloud/transfers?limit=200${filter ? `&status=${filter}` : ""}`
        ),
      ]);
      setQueue(q);
      setPage(p);
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const hasActive = !!(queue && (queue.pending > 0 || queue.running > 0));
  const timerRef = useRef<number | null>(null);
  useEffect(() => {
    if (!auto || !hasActive) return;
    timerRef.current = window.setTimeout(refresh, 5000);
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [auto, hasActive, page, refresh]);

  async function retry(id: number) {
    try {
      await api.post(`/api/pcloud/transfers/${id}/retry`);
      toast.success("已重新排入佇列");
      refresh();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function cancel(id: number) {
    try {
      await api.post(`/api/pcloud/transfers/${id}/cancel`);
      toast.success("已取消");
      refresh();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function cleanup(keepFailed: boolean) {
    const verb = keepFailed ? "已完成 + 已取消" : "已完成 + 已取消 + 失敗";
    const ok = await confirmDialog(`清掉 ${verb} 的紀錄?`);
    if (!ok) return;
    try {
      const res = await api.post<{ deleted: number }>(
        "/api/pcloud/transfers/cleanup",
        { keep_failed: keepFailed }
      );
      toast.success(`已刪除 ${res.deleted} 筆`);
      refresh();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" onClick={refresh} disabled={!loggedIn}>
          <RotateCw aria-hidden />
          {loading ? "更新中…" : "重新整理"}
        </Button>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Checkbox
            checked={auto}
            onCheckedChange={(v) => setAuto(v === true)}
          />
          有任務時自動更新 (5s)
        </label>
        {queue && (
          <div className="ml-auto text-xs text-muted-foreground">
            佇列 {queue.running} / {queue.concurrency} ・ 排隊 {queue.pending}
          </div>
        )}
      </div>

      <QueueBar queue={queue} onCleanup={cleanup} />

      <div className="flex flex-wrap gap-1">
        {STATUS_FILTERS.map((s) => (
          <Button
            key={s.key || "all"}
            size="sm"
            variant={filter === s.key ? "default" : "ghost"}
            onClick={() => setFilter(s.key)}
          >
            {s.label}
            {page && s.key && (
              <span className="opacity-60">({(page as any)[s.key] ?? 0})</span>
            )}
          </Button>
        ))}
      </div>

      <TransfersTable
        items={page?.items ?? []}
        onRetry={retry}
        onCancel={cancel}
      />
    </div>
  );
}
