"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

type Progress = {
  current: number;
  kind: "file" | "folder";
  action: "move" | "skip" | "error";
  source: string;
  code?: string | null;
  listing_kind?: string | null;
  listing_name?: string | null;
  target_path?: string | null;
  target_name?: string | null;
  would_create?: boolean;
  reason?: string | null;
};

type Result = {
  total: number;
  moved: number;
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

const ACTION_LABEL: Record<Progress["action"], { text: string; cls: string }> = {
  move: { text: "📦 歸類", cls: "text-emerald-300" },
  skip: { text: "⏭ 略過", cls: "text-white/50" },
  error: { text: "✗ 失敗", cls: "text-red-300" },
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

const STATUS_LABEL: Record<JobStatus, { text: string; cls: string }> = {
  running: { text: "執行中", cls: "text-amber-300" },
  done: { text: "已完成", cls: "text-emerald-300" },
  error: { text: "錯誤", cls: "text-red-300" },
  cancelled: { text: "已取消", cls: "text-white/60" },
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
      const data = await api.get<Job>(
        `/api/pcloud/files/organize/jobs/${id}?since=${sinceRef.current}`
      );
      sinceRef.current = data.next_since;
      // Merge new events onto whatever we already have rather than
      // replacing — the response only carries the tail since `since`.
      setJob((prev) => {
        if (!prev || prev.job_id !== id) {
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
  const percent =
    job && job.total ? Math.round((job.events.length / job.total) * 100) : 0;
  const recent = job ? job.events.slice(-10).reverse() : [];
  const processing = job?.processing ?? null;

  return (
    <>
      <button
        className="btn-ghost relative disabled:opacity-30"
        onClick={() => setOpen(true)}
        disabled={disabled}
        title={
          disabled
            ? "根目錄不可歸類，請先進入子資料夾"
            : "依番號自動搬到 AVBT/<系列>/<追蹤名稱>/ 之下"
        }
      >
        📦 歸類此資料夾
        {activeBg && (
          <span
            className="absolute -top-1 -right-1 h-2.5 w-2.5 animate-pulse rounded-full bg-amber-400"
            title="此資料夾有歸類任務在背景執行中"
          />
        )}
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
          onClick={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div className="w-full max-w-xl space-y-4 rounded-xl border border-white/10 bg-panel p-5">
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold">
                歸類「{folder_name}」
              </h2>
              {job && (
                <span
                  className={`rounded px-2 py-0.5 text-xs ${STATUS_LABEL[job.status].cls}`}
                >
                  {STATUS_LABEL[job.status].text}
                </span>
              )}
              <button
                className="ml-auto text-white/40 hover:text-white"
                onClick={close}
                title={
                  busy
                    ? "關閉視窗但工作會在背景繼續，下次開啟會自動接回"
                    : "關閉"
                }
              >
                ✕
              </button>
            </div>

            <p className="text-xs text-white/50">
              只動此資料夾的直接子項目。對每個有番號的影片 / 資料夾，依 JavBus 查到的{" "}
              <span className="font-mono">系列 → 發行商 → 製作商</span>{" "}
              順序取第一個有的,搬到 <span className="font-mono">AVBT/&lt;類別&gt;/&lt;名稱&gt;/</span>。
              不需先追蹤,JavBus 完全查無資料才會略過。
              {" "}
              <span className="text-amber-300/70">關掉視窗工作會在背景繼續執行</span>。
            </p>

            {!job && (
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(e) => setDryRun(e.target.checked)}
                />
                <span>只預覽（不實際修改，也不建立目標資料夾）</span>
              </label>
            )}

            {error && (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            )}

            {job && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs text-white/60">
                  <span>
                    {job.events.length} / {job.total}{job.total > 0 ? ` (${percent}%)` : ""}
                    {job.dry_run && " ・ 預覽模式"}
                  </span>
                  <span>
                    歸類 {job.events.filter((p) => p.action === "move").length} ／
                    略過 {job.events.filter((p) => p.action === "skip").length} ／
                    失敗 {job.events.filter((p) => p.action === "error").length}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded bg-white/10">
                  <div
                    className="h-full bg-accent transition-[width]"
                    style={{ width: `${percent}%` }}
                  />
                </div>
                {processing && (
                  <div className="flex items-center gap-2 rounded-md border border-amber-400/20 bg-amber-400/5 px-2 py-1 text-xs text-amber-200/80">
                    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-amber-400" />
                    <span>
                      ⏳ 正在查 JavBus（{processing.current}/{job.total}）：
                    </span>
                    <span className="truncate font-mono">{processing.source}</span>
                  </div>
                )}
                <ul className="max-h-72 overflow-y-auto rounded-md border border-white/10 bg-ink/50 p-2 text-xs">
                  {recent.length === 0 && !processing && (
                    <li className="text-white/40">等待第一筆…</li>
                  )}
                  {recent.map((p) => {
                    const lbl = ACTION_LABEL[p.action];
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
                      <li
                        key={p.current}
                        className="flex flex-col gap-0.5 py-0.5"
                      >
                        <div className="flex items-baseline gap-2">
                          <span className={lbl.cls}>
                            {lbl.text}
                            {reasonTxt}
                          </span>
                          <span className="truncate text-white/60">
                            {p.kind === "folder" ? "📁 " : "📄 "}
                            {p.source}
                          </span>
                        </div>
                        {p.action === "move" && p.target_path && (
                          <div className="ml-8 flex items-baseline gap-1 text-white/50">
                            <span className="text-white/30">→</span>
                            {kindTag && (
                              <span className="rounded bg-emerald-500/10 px-1 text-[10px] text-emerald-300">
                                {kindTag}
                              </span>
                            )}
                            <span className="truncate font-mono text-accent">
                              {p.target_path}
                              {p.target_name ? `/${p.target_name}` : ""}
                            </span>
                            {p.would_create && (
                              <span className="text-[10px] text-amber-300/80">
                                （將建立）
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
              <div className="space-y-1 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
                <div>
                  共 <strong>{job.result.total}</strong> 個項目
                  {job.result.dry_run && (
                    <span className="ml-2 text-amber-300/80">（僅預覽，未修改）</span>
                  )}
                </div>
                <div className="text-emerald-300">📦 已歸類 {job.result.moved}</div>
                <div className="text-white/60">⏭ 略過 {job.result.skipped}</div>
                {job.result.errors > 0 && (
                  <div className="text-red-300">✗ 失敗 {job.result.errors}</div>
                )}
              </div>
            )}

            {job?.error && (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {job.error}
              </div>
            )}

            <div className="flex justify-end gap-2">
              {busy ? (
                <button className="btn-ghost" onClick={cancel}>
                  取消任務
                </button>
              ) : (
                <>
                  <button className="btn-ghost" onClick={close}>
                    關閉
                  </button>
                  {!job && (
                    <button className="btn-primary" onClick={submit}>
                      {dryRun ? "預覽" : "執行"}
                    </button>
                  )}
                  {job && job.status !== "running" && (
                    <button
                      className="btn-primary"
                      onClick={() => {
                        setJob(null);
                        setJobId(null);
                        setError(null);
                      }}
                    >
                      再來一次
                    </button>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
