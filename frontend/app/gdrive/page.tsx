"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, HardDrive, Pause, Play } from "lucide-react";
import { confirmDialog, toast } from "@/components/Toast";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Button } from "@/components/ui/button";
import {
  TRACKED_LABELS,
  api,
  type GDriveJob,
  type GDriveJobsOut,
  type GDriveTransfer,
} from "@/lib/api";
import { fmtBytes, fmtDateTime } from "@/lib/format";

const JOB_LABEL: Record<GDriveJob["status"], { text: string; cls: string }> = {
  scanning: { text: "掃描中", cls: "text-blue-300" },
  transferring: { text: "搬移中", cls: "text-blue-300" },
  done: { text: "完成", cls: "text-emerald-300" },
  partial: { text: "部分失敗", cls: "text-amber-300" },
  cancelled: { text: "已取消", cls: "text-muted-foreground" },
  error: { text: "錯誤", cls: "text-destructive" },
};

const SOURCE_LABEL: Record<GDriveJob["source_state"], string> = {
  keep: "PikPak 原檔保留",
  pending: "完成後刪 PikPak 原檔",
  trashed: "PikPak 原檔已丟垃圾桶",
  trash_failed: "PikPak 原檔刪除失敗",
};

const ACTIVE = new Set(["scanning", "transferring"]);

// Google Drive 搬移佇列:一家製作商一列,展開看各檔案進度。
export default function GDrivePage() {
  const [data, setData] = useState<GDriveJobsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [files, setFiles] = useState<GDriveTransfer[] | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.get<GDriveJobsOut>("/api/gdrive/jobs"));
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  const loadFiles = useCallback(async (jobId: number) => {
    try {
      setFiles(await api.get<GDriveTransfer[]>(`/api/gdrive/jobs/${jobId}/transfers`));
    } catch (e: any) {
      toast.error(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 有進行中的工作時每 5 秒更新。
  const hasActive = !!data?.jobs.some((j) => ACTIVE.has(j.status));
  useEffect(() => {
    if (!hasActive) return;
    const t = setInterval(() => {
      load();
      if (open !== null) loadFiles(open);
    }, 5000);
    return () => clearInterval(t);
  }, [hasActive, load, loadFiles, open]);

  function toggle(jobId: number) {
    if (open === jobId) {
      setOpen(null);
      setFiles(null);
    } else {
      setOpen(jobId);
      setFiles(null);
      loadFiles(jobId);
    }
  }

  async function act(path: string, okText: string, confirmText?: string) {
    if (confirmText && !(await confirmDialog(confirmText))) return;
    try {
      await api.post(path);
      toast.success(okText);
      await load();
      if (open !== null) await loadFiles(open);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function setPaused(paused: boolean) {
    try {
      await api.put("/api/gdrive/policy", { paused });
      await load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  if (error) return <ErrorBox message={error} />;
  if (!data) return <div className="text-sm text-muted-foreground">讀取中…</div>;

  const q = data.queue;
  const policy = q.policy;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="inline-flex items-center gap-2 text-xl font-semibold">
          <HardDrive className="h-5 w-5 text-primary" aria-hidden /> Google Drive 搬移
        </h1>
        <span className="text-sm text-muted-foreground">
          排隊 {q.pending}・上傳中 {q.running}・完成 {q.done}・失敗 {q.failed}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => setPaused(!policy.paused)}>
            {policy.paused ? (
              <>
                <Play className="h-3.5 w-3.5" aria-hidden /> 繼續
              </>
            ) : (
              <>
                <Pause className="h-3.5 w-3.5" aria-hidden /> 暫停
              </>
            )}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => act("/api/gdrive/jobs/cleanup", "已清除完成的紀錄", "清除已完成 / 取消 / 錯誤的工作紀錄?(Drive 位置索引會保留)")}
          >
            清除已完成
          </Button>
          <Link href="/studios" className="text-sm text-primary hover:underline">
            到製作商頁選擇
          </Link>
        </div>
      </div>

      <div className="rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground">
        今日已搬 {fmtBytes(policy.used_today_bytes, "0")}
        {policy.remaining_today_bytes !== null &&
          ` / 預算 ${policy.daily_budget_gb} GB(剩 ${fmtBytes(policy.remaining_today_bytes, "0")})`}
        {policy.window && `・允許時段 ${policy.window}`}
        {policy.blocked_reason && (
          <span className="ml-2 text-amber-300">目前暫停:{policy.blocked_reason}</span>
        )}
        <Link href="/settings" className="ml-2 text-primary hover:underline">
          調整
        </Link>
      </div>

      {data.jobs.length === 0 ? (
        <EmptyState
          icon={HardDrive}
          title="還沒有搬移工作"
          hint="到追蹤頁勾選「存到 Drive」,或到製作商頁按「搬到 Drive」"
        />
      ) : (
        <div className="space-y-2">
          {data.jobs.map((j) => {
            const label = JOB_LABEL[j.status];
            const pct = j.total_bytes ? Math.min(100, (j.uploaded_bytes / j.total_bytes) * 100) : 0;
            const failed = (j.counts.failed ?? 0) + (j.counts.cancelled ?? 0);
            const doneFiles = j.counts.done ?? 0;
            return (
              <div key={j.id} className="rounded-lg border border-border bg-card">
                <div className="flex flex-wrap items-center gap-3 p-3">
                  <div className="inline-flex items-center gap-1 font-medium">
                    <button
                      onClick={() => toggle(j.id)}
                      aria-label={open === j.id ? "收合檔案列表" : "展開檔案列表"}
                      className="text-muted-foreground hover:text-primary"
                    >
                      {open === j.id ? (
                        <ChevronDown className="h-4 w-4" aria-hidden />
                      ) : (
                        <ChevronRight className="h-4 w-4" aria-hidden />
                      )}
                    </button>
                    <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-normal text-muted-foreground">
                      {TRACKED_LABELS[j.listing_kind] ?? j.listing_kind}
                    </span>
                    <Link
                      href={
                        j.listing_kind === "studio"
                          ? `/studios/${encodeURIComponent(j.studio_id)}`
                          : `/${j.listing_kind}/${encodeURIComponent(j.studio_id)}`
                      }
                      className="hover:text-primary hover:underline"
                    >
                      {j.studio_name || j.studio_id}
                    </Link>
                    {j.origin === "auto" && (
                      <span
                        className="rounded bg-sky-400/15 px-1.5 py-0.5 text-[11px] font-normal text-sky-300"
                        title="由追蹤頁「存到 Drive」自動建立"
                      >
                        自動
                      </span>
                    )}
                  </div>
                  <span className="text-xs text-muted-foreground">→ {j.account_label}</span>
                  <span className={`text-xs ${label.cls}`}>{label.text}</span>
                  <span className="text-xs text-muted-foreground">{SOURCE_LABEL[j.source_state]}</span>
                  <div className="ml-auto flex flex-wrap gap-2">
                    {failed > 0 && (
                      <Button size="sm" variant="outline" onClick={() => act(`/api/gdrive/jobs/${j.id}/retry`, "已重新排隊")}>
                        重試失敗({failed})
                      </Button>
                    )}
                    {j.status === "done" && j.source_state === "trash_failed" && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => act(`/api/gdrive/jobs/${j.id}/trash-source`, "PikPak 原檔已丟垃圾桶")}
                      >
                        重試刪除原檔
                      </Button>
                    )}
                    {j.status === "done" && j.source_state === "keep" && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          act(
                            `/api/gdrive/jobs/${j.id}/trash-source`,
                            "PikPak 原檔已丟垃圾桶",
                            `把「${j.studio_name}」在 PikPak 的原檔丟到垃圾桶?`
                          )
                        }
                      >
                        刪除 PikPak 原檔
                      </Button>
                    )}
                    {ACTIVE.has(j.status) && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          act(`/api/gdrive/jobs/${j.id}/cancel`, "已取消", `取消「${j.studio_name}」的搬移?已上傳的檔案會留在 Drive。`)
                        }
                      >
                        取消
                      </Button>
                    )}
                  </div>
                </div>
                <div className="space-y-1 px-3 pb-3">
                  <div className="h-1.5 w-full overflow-hidden rounded bg-muted">
                    <div
                      className={"h-full " + (j.status === "partial" ? "bg-amber-400" : "bg-primary")}
                      style={{ width: `${j.status === "done" ? 100 : pct}%` }}
                    />
                  </div>
                  <div className="flex flex-wrap gap-x-4 text-xs text-muted-foreground">
                    <span>
                      {j.code_count} 部・{doneFiles}/{j.file_count} 檔
                    </span>
                    <span>
                      {fmtBytes(j.uploaded_bytes, "0")} / {fmtBytes(j.total_bytes, "0")}
                    </span>
                    <span>建立 {fmtDateTime(j.created_at)}</span>
                    {j.finished_at && <span>結束 {fmtDateTime(j.finished_at)}</span>}
                    {j.message && j.message !== SOURCE_LABEL[j.source_state] && (
                      <span className="text-foreground/70">{j.message}</span>
                    )}
                  </div>
                </div>
                {open === j.id && (
                  <div className="max-h-96 overflow-y-auto border-t border-border px-3 py-2 text-xs">
                    {files === null ? (
                      <div className="text-muted-foreground">讀取中…</div>
                    ) : files.length === 0 ? (
                      <div className="text-muted-foreground">沒有檔案</div>
                    ) : (
                      files.map((f) => (
                        <div key={f.id} className="flex items-center gap-2 py-0.5">
                          <span className="w-16 shrink-0 font-mono text-primary">{f.code}</span>
                          <span className="min-w-0 flex-1 truncate font-mono text-foreground/80" title={f.pikpak_path}>
                            {f.name}
                          </span>
                          <span className="shrink-0 text-muted-foreground">
                            {f.status === "done" ? fmtBytes(f.size) : `${fmtBytes(f.bytes_uploaded, "0")} / ${fmtBytes(f.size)}`}
                          </span>
                          <span
                            className={
                              "w-56 shrink-0 truncate " +
                              (f.status === "failed"
                                ? "text-destructive"
                                : f.status === "done"
                                  ? "text-emerald-300"
                                  : "text-muted-foreground")
                            }
                            title={f.message}
                          >
                            {f.message}
                          </span>
                          {(f.status === "failed" || f.status === "cancelled") && (
                            <button
                              onClick={() => act(`/api/gdrive/transfers/${f.id}/retry`, "已重新排隊")}
                              className="shrink-0 text-primary hover:underline"
                            >
                              重試
                            </button>
                          )}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
