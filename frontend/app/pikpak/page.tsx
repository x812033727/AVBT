"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RotateCw } from "lucide-react";
import DownloadQueuePanel from "@/components/DownloadQueuePanel";
import ArchiverBar from "@/components/pikpak/ArchiverBar";
import PikPakFilesSection from "@/components/pikpak/PikPakFilesSection";
import TasksTable from "@/components/pikpak/TasksTable";
import { confirmDialog, toast } from "@/components/Toast";
import VideoPlayerModal from "@/components/VideoPlayerModal";
import {
  api,
  type ArchiverStatus,
  type PikPakFile,
  type PikPakQuota,
  type PikPakTask,
} from "@/lib/api";
import { fmtBytes } from "@/lib/format";
import { isVideo } from "@/lib/video";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

const ACTIVE_PHASES = new Set([
  "PHASE_TYPE_PENDING",
  "PHASE_TYPE_RUNNING",
  "PHASE_TYPE_QUEUED",
]);

export default function PikpakPage() {
  const [tab, setTab] = useState<"tasks" | "files">("tasks");
  const [quota, setQuota] = useState<PikPakQuota | null>(null);
  const [tasks, setTasks] = useState<PikPakTask[]>([]);
  const [files, setFiles] = useState<PikPakFile[]>([]);
  const [parents, setParents] = useState<{ id: string; name: string }[]>([
    { id: "", name: "我的雲盤" },
  ]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const [playing, setPlaying] = useState<PikPakFile | null>(null);
  const [archiver, setArchiver] = useState<ArchiverStatus | null>(null);

  const loadTasks = useCallback(async () => {
    setError(null);
    try {
      const [q, t, a] = await Promise.all([
        api.get<PikPakQuota>("/api/pikpak/quota").catch(() => null),
        api.get<PikPakTask[]>("/api/pikpak/tasks"),
        api.get<ArchiverStatus>("/api/pikpak/archiver").catch(() => null),
      ]);
      setQuota(q);
      setTasks(t);
      setArchiver(a);
    } catch (e: any) {
      setError(e.message);
      toast.error(e.message);
    }
  }, []);

  async function toggleArchiver(enabled: boolean) {
    try {
      const a = await api.post<ArchiverStatus>("/api/pikpak/archiver/toggle", {
        enabled,
      });
      setArchiver(a);
      toast.success(enabled ? "已開啟自動歸檔" : "已關閉自動歸檔");
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function runArchiverNow() {
    try {
      const a = await api.post<ArchiverStatus & { moved: number }>(
        "/api/pikpak/archiver/run"
      );
      setArchiver(a);
      if (a.moved) {
        toast.success(`已歸檔 ${a.moved} 個檔案`);
        loadTasks();
      } else {
        toast.info("沒有可歸檔的檔案");
      }
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function sweepNow() {
    try {
      const a = await api.post<ArchiverStatus & { moved: number }>(
        "/api/pikpak/archiver/sweep"
      );
      setArchiver(a);
      if (a.moved) {
        toast.success(`掃描 TASK 完成，搬移 ${a.moved} 個`);
        loadTasks();
      } else {
        toast.info("TASK 沒有待搬移的項目");
      }
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const failedCount = useMemo(
    () =>
      tasks.filter(
        (t) => t.phase === "PHASE_TYPE_ERROR" || t.phase === "ERROR"
      ).length,
    [tasks]
  );

  async function cleanupFailed() {
    if (!failedCount) return;
    const ok = await confirmDialog(`清理 ${failedCount} 個失敗任務？`, "不會刪除已下載的檔案");
    if (!ok) return;
    try {
      const res = await api.post<{ deleted: number }>(
        "/api/pikpak/tasks/cleanup-failed"
      );
      toast.success(`已清理 ${res.deleted} 個失敗任務`);
      loadTasks();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const currentParent = parents[parents.length - 1].id;

  const loadFiles = useCallback(
    async (parentId: string) => {
      setError(null);
      setLoading(true);
      try {
        const res = await api.get<PikPakFile[]>(
          `/api/pikpak/files?parent_id=${encodeURIComponent(parentId)}`
        );
        setFiles(res);
      } catch (e: any) {
        setError(e.message);
        toast.error(e.message);
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    if (tab === "tasks") loadTasks();
    else loadFiles(currentParent);
  }, [tab, currentParent, loadTasks, loadFiles]);

  // Auto-refresh tasks every 8s if any task is still running.
  const timerRef = useRef<number | null>(null);
  useEffect(() => {
    if (tab !== "tasks" || !auto) return;
    const hasActive = tasks.some((t) => ACTIVE_PHASES.has(t.phase));
    if (!hasActive) return;
    timerRef.current = window.setTimeout(loadTasks, 8000);
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [tab, tasks, auto, loadTasks]);

  async function openFolder(f: PikPakFile) {
    if (f.kind !== "drive#folder") {
      if (isVideo(f.name)) {
        setPlaying(f);
        return;
      }
      try {
        const { url } = await api.get<{ url: string }>(
          `/api/pikpak/files/${f.id}/url`
        );
        if (url) window.open(url, "_blank");
      } catch (e: any) {
        toast.error(e.message || "讀取連結失敗");
      }
      return;
    }
    setSearch("");
    setParents([...parents, { id: f.id, name: f.name }]);
  }

  function gotoCrumb(idx: number) {
    setSearch("");
    setParents(parents.slice(0, idx + 1));
  }

  async function deleteTasks(ids: string[]) {
    if (!ids.length) return;
    const ok = await confirmDialog(`刪除 ${ids.length} 個任務？`);
    if (!ok) return;
    try {
      await api.post("/api/pikpak/tasks/delete", {
        task_ids: ids,
        delete_files: false,
      });
      toast.success(`已刪除 ${ids.length} 個任務`);
      loadTasks();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function retryTask(id: string) {
    try {
      await api.post(`/api/pikpak/tasks/${id}/retry`);
      toast.success("已重試任務");
      loadTasks();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function trashFiles(ids: string[]) {
    if (!ids.length) return;
    const ok = await confirmDialog(`移到垃圾桶 ${ids.length} 個檔案？`);
    if (!ok) return;
    try {
      await api.post("/api/pikpak/files/trash", { ids });
      toast.success(`已將 ${ids.length} 個檔案移到垃圾桶`);
      loadFiles(currentParent);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function shareFiles(ids: string[]) {
    try {
      const res = await api.post<{ url: string; pass_code: string }>(
        "/api/pikpak/share",
        { file_ids: ids }
      );
      if (res.url) {
        const text = res.url + (res.pass_code ? ` (碼: ${res.pass_code})` : "");
        try {
          await navigator.clipboard.writeText(text);
          toast.success("分享連結已複製到剪貼簿");
        } catch {
          toast.info(`分享連結：${res.url}`);
        }
      }
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function runSearch() {
    if (!search.trim()) {
      loadFiles(currentParent);
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const res = await api.get<PikPakFile[]>(
        `/api/pikpak/files/search?q=${encodeURIComponent(search.trim())}&parent_id=${encodeURIComponent(currentParent)}`
      );
      setFiles(res);
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Tabs value={tab} onValueChange={(v) => setTab(v as "tasks" | "files")}>
          <TabsList>
            <TabsTrigger value="tasks">離線任務</TabsTrigger>
            <TabsTrigger value="files">雲端檔案</TabsTrigger>
          </TabsList>
        </Tabs>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => (tab === "tasks" ? loadTasks() : loadFiles(currentParent))}
        >
          <RotateCw aria-hidden />
          {loading ? "更新中…" : "重新整理"}
        </Button>
        {tab === "tasks" && (
          <>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Checkbox
                checked={auto}
                onCheckedChange={(v) => setAuto(v === true)}
              />
              自動更新 (8s)
            </label>
            {failedCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={cleanupFailed}
                className="text-red-300 hover:bg-red-500/10 hover:text-red-300"
                title="刪除所有失敗的任務（不會刪除已下載的檔案）"
              >
                清理失敗 ({failedCount})
              </Button>
            )}
          </>
        )}
        {quota && (
          <div className="ml-auto text-xs text-muted-foreground">
            已用 {fmtBytes(quota.used)} / {fmtBytes(quota.limit)}
          </div>
        )}
      </div>

      {error && <ErrorBox message={error} />}

      {tab === "tasks" && archiver && (
        <ArchiverBar
          archiver={archiver}
          onToggle={toggleArchiver}
          onSweep={sweepNow}
          onRunNow={runArchiverNow}
          onReload={loadTasks}
        />
      )}

      {tab === "tasks" && <DownloadQueuePanel />}

      {tab === "tasks" && (
        <TasksTable tasks={tasks} onDelete={deleteTasks} onRetry={retryTask} />
      )}

      {tab === "files" && (
        <PikPakFilesSection
          files={files}
          parents={parents}
          currentParent={currentParent}
          loading={loading}
          search={search}
          onSearch={setSearch}
          onSubmitSearch={runSearch}
          onOpen={openFolder}
          onCrumb={gotoCrumb}
          onTrash={trashFiles}
          onShare={shareFiles}
          onRefresh={() => loadFiles(currentParent)}
        />
      )}

      <VideoPlayerModal
        open={!!playing}
        file={playing ? { id: playing.id, name: playing.name } : null}
        onClose={() => setPlaying(null)}
      />
    </div>
  );
}
