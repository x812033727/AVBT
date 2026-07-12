"use client";

import { useEffect, useRef, useState } from "react";
import {
  File,
  FileOutput,
  Folder,
  Package,
  SkipForward,
  X,
  type LucideIcon,
} from "lucide-react";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress as ProgressBar } from "@/components/ui/progress";
import { api } from "@/lib/api";
import type { StatusTone } from "@/lib/status";

type Progress = {
  current: number;
  kind: "file" | "folder";
  action: "move" | "flatten" | "skip" | "error";
  source: string;
  code?: string | null;
  listing_kind?: string | null;
  listing_name?: string | null;
  target_path?: string | null;
  target_name?: string | null;
  // Number of items trashed alongside the keeper when flattening a
  // wrapper folder — surfaced so the user sees what was discarded.
  extras_count?: number;
  would_create?: boolean;
  // Set on a flatten when JavBus couldn't categorise the code: the
  // video was pulled out of its wrapper *in place* rather than moved
  // under AVBT/<類別>/<名稱>/.
  uncategorized?: boolean;
  reason?: string | null;
};

type Result = {
  total: number;
  moved: number;
  flattened: number;
  skipped: number;
  errors: number;
  dry_run: boolean;
};

type JobStatus = "running" | "done" | "error" | "cancelled";

type Job = {
  job_id: string;
  folder_id: string;
  folder_name: string;
  dry_run: boolean;
  status: JobStatus;
  started_at: string;
  finished_at: string | null;
  total: number;
  processing: { current: number; source: string; kind: string } | null;
  events: Progress[];
  next_since: number;
  result: Result | null;
  error: string | null;
};

const ACTION_LABEL: Record<
  Progress["action"],
  { icon: LucideIcon; text: string; cls: string }
> = {
  move: { icon: Package, text: "歸類", cls: "text-emerald-300" },
  flatten: { icon: FileOutput, text: "取出主檔", cls: "text-sky-300" },
  skip: { icon: SkipForward, text: "略過", cls: "text-muted-foreground/70" },
  error: { icon: X, text: "失敗", cls: "text-red-300" },
};

const REASON_LABEL: Record<string, string> = {
  no_code: "無法辨識番號",
  no_listing: "JavBus 查無系列 / 發行商 / 製作商",
  // legacy — kept so old finished jobs still render their reason
  no_tracked_match: "無追蹤對應",
  already_organized: "已在目標資料夾",
};

const KIND_LABEL: Record<string, string> = {
  series: "系列",
  star: "女優",
  studio: "製作商",
  label: "發行商",
  director: "導演",
};

const STATUS_LABEL: Record<JobStatus, { text: string; tone: StatusTone }> = {
  running: { text: "執行中", tone: "warning" },
  done: { text: "已完成", tone: "success" },
  error: { text: "錯誤", tone: "danger" },
  cancelled: { text: "已取消", tone: "muted" },
};

export default function PCloudOrganizeButton({
  folder_id,
  folder_name,
  onDone,
  disabled,
}: {
  folder_id: string;
  folder_name: string;
  onDone?: () => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Lightweight indicator on the button when a background job is
  // running for this folder, even though the modal is closed.
  const [activeBg, setActiveBg] = useState(false);

  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sinceRef = useRef(0);
  const previousStatusRef = useRef<JobStatus | null>(null);

  function stopPolling() {
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }

  // Background-job indicator: when no modal is open, check periodically
  // whether this folder has a running job (e.g. one started before the
  // modal was closed, or in another browser tab). Light touch — 5s.
  useEffect(() => {
    if (open) {
      setActiveBg(false);
      return;
    }
    let cancelled = false;
    async function tick() {
      try {
        const jobs = await api.get<Job[]>(
          `/api/pcloud/files/organize/jobs?folder_id=${encodeURIComponent(
            folder_id
          )}&status=running`
        );
        if (!cancelled) setActiveBg(jobs.length > 0);
      } catch {
        if (!cancelled) setActiveBg(false);
      }
    }
    tick();
    const h = setInterval(tick, 5000);
    return () => {
      cancelled = true;
      clearInterval(h);
    };
  }, [open, folder_id]);

  async function pollOnce(id: string) {
    try {
      // Snapshot the cursor we're fetching from. A fresh (re)attach
      // resets it to 0 in startPolling, so `from === 0` means "this
      // response is the FULL event list", not a tail.
      const from = sinceRef.current;
      const data = await api.get<Job>(
        `/api/pcloud/files/organize/jobs/${id}?since=${from}`
      );
      sinceRef.current = data.next_since;
      // Merge the tail onto what we already have. But when `from === 0`
      // the response carries every event from the start, so we must
      // REPLACE rather than append — otherwise reopening the modal
      // mid-run (which the UI encourages: "關掉視窗工作會在背景繼續執行")
      // re-appends all prior events onto the retained job state and the
      // 已處理 count / action tallies / progress bar double-count (爆掉).
      setJob((prev) => {
        if (from === 0 || !prev || prev.job_id !== id) {
          return { ...data, events: data.events };
        }
        return {
          ...data,
          events: [...prev.events, ...data.events],
        };
      });
      if (data.status === "running") {
        pollRef.current = setTimeout(() => pollOnce(id), 1000);
      } else {
        // Terminal — fire onDone once for live (non-dry) success.
        if (
          previousStatusRef.current === "running" &&
          data.status === "done" &&
          !data.dry_run
        ) {
          onDone?.();
        }
        previousStatusRef.current = data.status;
      }
    } catch (e: any) {
      setError(e.message || "查詢任務失敗");
      stopPolling();
    }
  }

  function startPolling(id: string) {
    stopPolling();
    sinceRef.current = 0;
    previousStatusRef.current = "running";
    pollOnce(id);
  }

  async function submit() {
    setError(null);
    setJob(null);
    sinceRef.current = 0;
    try {
      const data = await api.post<{ job_id: string; status: string }>(
        "/api/pcloud/files/organize/jobs",
        { folder_id, folder_name, dry_run: dryRun }
      );
      setJobId(data.job_id);
      startPolling(data.job_id);
    } catch (e: any) {
      // 409 = active job already — auto-attach to it instead of erroring.
      const msg = e?.message || "";
      const match = msg.match(/job_id['":\s]+([0-9a-f]{6,})/i);
      if (match) {
        setJobId(match[1]);
        startPolling(match[1]);
        return;
      }
      setError(msg || "建立任務失敗");
    }
  }

  async function cancel() {
    if (!jobId) return;
    try {
      await api.post(`/api/pcloud/files/organize/jobs/${jobId}/cancel`, {});
    } catch {
      /* polling will reflect terminal state */
    }
  }

  function close() {
    // Modal close does NOT stop the job — that's the whole point of
    // the background-task refactor. We just stop polling locally.
    stopPolling();
    setOpen(false);
    // Keep error / jobId so the next open can resume cleanly.
    if (!job || job.status !== "running") {
      setJob(null);
      setJobId(null);
      setError(null);
      setDryRun(true);
    }
  }

  // On open: if there's already an active job for this folder, attach
  // to it instead of showing the submit form. This is the "I closed
  // the tab, came back, want to see the progress" path.
  useEffect(() => {
    if (!open) {
      stopPolling();
      return;
    }
    if (jobId) {
      // Already attached to a job (e.g. user reopened mid-run).
      startPolling(jobId);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const jobs = await api.get<Job[]>(
          `/api/pcloud/files/organize/jobs?folder_id=${encodeURIComponent(
            folder_id
          )}&status=running`
        );
        if (cancelled) return;
        if (jobs.length > 0) {
          setJobId(jobs[0].job_id);
          startPolling(jobs[0].job_id);
        }
      } catch {
        /* not fatal — user can still kick off a new job */
      }
    })();
    return () => {
      cancelled = true;
      stopPolling();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const busy = job?.status === "running";
  // A single folder child can fan out into several extraction events, so
  // events.length may exceed total (which counts direct children) — clamp
  // so the bar / label never reads above 100%.
  const percent =
    job && job.total
      ? Math.min(100, Math.round((job.events.length / job.total) * 100))
      : 0;
  const recent = job ? job.events.slice(-10).reverse() : [];
  const processing = job?.processing ?? null;

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        className="relative"
        onClick={() => setOpen(true)}
        disabled={disabled}
        title={
          disabled
            ? "根目錄不可歸類，請先進入子資料夾"
            : "依番號自動搬到 AVBT/<系列>/<追蹤名稱>/ 之下"
        }
      >
        <Package aria-hidden />
        歸類此資料夾
        {activeBg && (
          <span
            className="absolute -right-1 -top-1 h-2.5 w-2.5 animate-pulse rounded-full bg-amber-400"
            title="此資料夾有歸類任務在背景執行中"
          />
        )}
      </Button>

      <Dialog
        open={open}
        onOpenChange={(v) => {
          if (!v) close();
        }}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              歸類「{folder_name}」
              {job && (
                <StatusBadge tone={STATUS_LABEL[job.status].tone}>
                  {STATUS_LABEL[job.status].text}
                </StatusBadge>
              )}
            </DialogTitle>
          </DialogHeader>

          <p className="text-xs text-muted-foreground">
            掃此資料夾的直接子項目。子資料夾會「鑽進去」(遞迴最多 6 層)把每支影片
            都挖出來,各自依番號改名為 <span className="font-mono">&lt;番號&gt;.&lt;副檔名&gt;</span>,
            依 JavBus 查到的 <span className="font-mono">系列 → 發行商 → 製作商</span>{" "}
            順序搬到 <span className="font-mono">AVBT/&lt;類別&gt;/&lt;名稱&gt;/</span>;
            同番號的重複 / 低畫質版本只留最大那支,其餘連同 sample / nfo / 種子
            隨包裝資料夾送進回收桶。資料夾名沒番號也會鑽進去借裡面影片的番號,
            一個資料夾裡有多部不同番號作品也會分別取出。
            {" "}
            <span className="text-amber-300/70">JavBus 查無分類時,影片照樣從子資料夾「原地取出」</span>,
            不會卡在裡面;查不到或搬移失敗的那支會保留,不會誤刪。
            {" "}
            <span className="text-amber-300/70">關掉視窗工作會在背景繼續執行</span>。
          </p>

          {!job && (
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={dryRun}
                onCheckedChange={(v) => setDryRun(v === true)}
              />
              <span>只預覽（不實際修改，也不建立目標資料夾）</span>
            </label>
          )}

          {error && <ErrorBox message={error} />}

          {job && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  {/* events can exceed total (one folder → many
                      extractions), so show "已處理 N" rather than a
                      possibly-odd "N / total". */}
                  已處理 {job.events.length}
                  {job.total > 0 ? ` / 共 ${job.total} 項 (${percent}%)` : ""}
                  {job.dry_run && " ・ 預覽模式"}
                </span>
                <span>
                  歸類 {job.events.filter((p) => p.action === "move").length} ／
                  取出 {job.events.filter((p) => p.action === "flatten").length} ／
                  略過 {job.events.filter((p) => p.action === "skip").length} ／
                  失敗 {job.events.filter((p) => p.action === "error").length}
                </span>
              </div>
              <ProgressBar value={percent} className="h-2" />
              {processing && (
                <div className="flex items-center gap-2 rounded-md border border-amber-400/20 bg-amber-400/5 px-2 py-1 text-xs text-amber-200/80">
                  <span
                    className="inline-block h-2 w-2 animate-pulse rounded-full bg-amber-400"
                    aria-hidden
                  />
                  <span>
                    正在查 JavBus（{processing.current}/{job.total}）：
                  </span>
                  <span className="truncate font-mono">{processing.source}</span>
                </div>
              )}
              <ul className="max-h-72 overflow-y-auto rounded-md border border-border bg-background/50 p-2 text-xs">
                {recent.length === 0 && !processing && (
                  <li className="text-muted-foreground/70">等待第一筆…</li>
                )}
                {recent.map((p) => {
                  const lbl = ACTION_LABEL[p.action];
                  const ActionIcon = lbl.icon;
                  const reasonTxt =
                    p.reason && REASON_LABEL[p.reason]
                      ? `（${REASON_LABEL[p.reason]}）`
                      : p.reason
                      ? `（${p.reason}）`
                      : "";
                  const kindTag = p.listing_kind
                    ? KIND_LABEL[p.listing_kind] || p.listing_kind
                    : null;
                  return (
                    <li key={p.current} className="flex flex-col gap-0.5 py-0.5">
                      <div className="flex items-baseline gap-2">
                        <span
                          className={`inline-flex items-center gap-1 ${lbl.cls}`}
                        >
                          <ActionIcon
                            className="h-3 w-3 shrink-0"
                            aria-hidden
                          />
                          {lbl.text}
                          {reasonTxt}
                        </span>
                        <span className="inline-flex min-w-0 items-center gap-1 truncate text-muted-foreground">
                          {p.kind === "folder" ? (
                            <Folder className="h-3 w-3 shrink-0" aria-hidden />
                          ) : (
                            <File className="h-3 w-3 shrink-0" aria-hidden />
                          )}
                          <span className="truncate">{p.source}</span>
                        </span>
                      </div>
                      {(p.action === "move" || p.action === "flatten") &&
                        (p.target_path || p.uncategorized) && (
                        <div className="ml-8 flex items-baseline gap-1 text-muted-foreground/70">
                          <span className="text-muted-foreground/40">→</span>
                          {p.uncategorized ? (
                            <>
                              <span className="rounded bg-amber-500/10 px-1 text-[10px] text-amber-300">
                                原地取出
                              </span>
                              <span className="truncate font-mono text-primary">
                                {p.target_name}
                              </span>
                              <span className="text-[10px] text-muted-foreground/60">
                                （JavBus 查無分類）
                              </span>
                            </>
                          ) : (
                            <>
                              {kindTag && (
                                <span className="rounded bg-emerald-500/10 px-1 text-[10px] text-emerald-300">
                                  {kindTag}
                                </span>
                              )}
                              <span className="truncate font-mono text-primary">
                                {p.target_path}
                                {p.target_name ? `/${p.target_name}` : ""}
                              </span>
                              {p.would_create && (
                                <span className="text-[10px] text-amber-300/80">
                                  （將建立）
                                </span>
                              )}
                            </>
                          )}
                          {p.action === "flatten" &&
                            typeof p.extras_count === "number" &&
                            p.extras_count > 0 && (
                              <span className="text-[10px] text-muted-foreground/60">
                                （清掉 {p.extras_count} 個額外項目）
                              </span>
                            )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {job?.result && (
            <div className="space-y-1 rounded-md border border-border bg-card px-3 py-2 text-sm">
              <div>
                共 <strong>{job.result.total}</strong> 個項目
                {job.result.dry_run && (
                  <span className="ml-2 text-amber-300/80">
                    （僅預覽，未修改）
                  </span>
                )}
              </div>
              <div className="flex items-center gap-1 text-emerald-300">
                <Package className="h-3.5 w-3.5" aria-hidden />
                已歸類 {job.result.moved}
              </div>
              {job.result.flattened > 0 && (
                <div className="flex items-center gap-1 text-sky-300">
                  <FileOutput className="h-3.5 w-3.5" aria-hidden />
                  已取出主檔 {job.result.flattened}
                </div>
              )}
              <div className="flex items-center gap-1 text-muted-foreground">
                <SkipForward className="h-3.5 w-3.5" aria-hidden />
                略過 {job.result.skipped}
              </div>
              {job.result.errors > 0 && (
                <div className="flex items-center gap-1 text-red-300">
                  <X className="h-3.5 w-3.5" aria-hidden />
                  失敗 {job.result.errors}
                </div>
              )}
            </div>
          )}

          {job?.error && <ErrorBox message={job.error} />}

          <DialogFooter>
            {busy ? (
              <Button variant="ghost" size="sm" onClick={cancel}>
                取消任務
              </Button>
            ) : (
              <>
                <Button variant="ghost" size="sm" onClick={close}>
                  關閉
                </Button>
                {!job && (
                  <Button size="sm" onClick={submit}>
                    {dryRun ? "預覽" : "執行"}
                  </Button>
                )}
                {job && job.status !== "running" && (
                  <Button
                    size="sm"
                    onClick={() => {
                      setJob(null);
                      setJobId(null);
                      setError(null);
                    }}
                  >
                    再來一次
                  </Button>
                )}
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
