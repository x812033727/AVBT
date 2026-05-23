"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { confirmDialog, toast } from "@/components/Toast";
import {
  api,
  type PCloudQueueStatus,
  type PCloudStatus,
  type PCloudTransfer,
  type PCloudTransferPage,
} from "@/lib/api";

function fmtBytes(n?: number | null) {
  if (!n) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(2)} ${u[i]}`;
}

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-white/10 text-white/70",
  running: "bg-blue-500/20 text-blue-200",
  done: "bg-emerald-400/20 text-emerald-200",
  failed: "bg-red-500/20 text-red-300",
  cancelled: "bg-white/5 text-white/40",
};

const STATUS_FILTERS = [
  { key: "", label: "全部" },
  { key: "pending", label: "等待中" },
  { key: "running", label: "傳輸中" },
  { key: "done", label: "完成" },
  { key: "failed", label: "失敗" },
  { key: "cancelled", label: "已取消" },
] as const;

export default function PCloudPage() {
  const [status, setStatus] = useState<PCloudStatus | null>(null);
  const [queue, setQueue] = useState<PCloudQueueStatus | null>(null);
  const [page, setPage] = useState<PCloudTransferPage | null>(null);
  const [filter, setFilter] = useState<string>("");
  const [auto, setAuto] = useState(true);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [s, q, p] = await Promise.all([
        api.get<PCloudStatus>("/api/pcloud/status"),
        api.get<PCloudQueueStatus>("/api/pcloud/queue"),
        api.get<PCloudTransferPage>(
          `/api/pcloud/transfers?limit=200${filter ? `&status=${filter}` : ""}`
        ),
      ]);
      setStatus(s);
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
        <h1 className="text-lg font-semibold">PikPak → pCloud 傳輸</h1>
        <button onClick={refresh} className="btn-ghost">
          {loading ? "更新中…" : "重新整理"}
        </button>
        <label className="flex items-center gap-1 text-xs text-white/60">
          <input
            type="checkbox"
            checked={auto}
            onChange={(e) => setAuto(e.target.checked)}
          />
          有任務時自動更新 (5s)
        </label>
        {queue && (
          <div className="ml-auto text-xs text-white/50">
            佇列 {queue.running} / {queue.concurrency} ・ 排隊 {queue.pending}
          </div>
        )}
      </div>

      <LoginPanel status={status} onChanged={refresh} />

      {status?.logged_in && (
        <>
          <QueueBar queue={queue} onCleanup={cleanup} />

          <div className="flex flex-wrap gap-1">
            {STATUS_FILTERS.map((s) => (
              <button
                key={s.key || "all"}
                onClick={() => setFilter(s.key)}
                className={
                  filter === s.key
                    ? "btn-primary text-xs"
                    : "btn-ghost text-xs"
                }
              >
                {s.label}
                {page && s.key && (
                  <span className="ml-1 text-white/40">
                    ({(page as any)[s.key] ?? 0})
                  </span>
                )}
              </button>
            ))}
          </div>

          <TransfersTable
            items={page?.items ?? []}
            onRetry={retry}
            onCancel={cancel}
          />
        </>
      )}
    </div>
  );
}

function LoginPanel({
  status,
  onChanged,
}: {
  status: PCloudStatus | null;
  onChanged: () => void;
}) {
  const [mode, setMode] = useState<"password" | "token">("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [showLogin, setShowLogin] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      const body =
        mode === "token"
          ? { access_token: token.trim() }
          : { username: username.trim(), password };
      await api.post<PCloudStatus>("/api/pcloud/login", body);
      toast.success("pCloud 登入成功");
      setUsername("");
      setPassword("");
      setToken("");
      setShowLogin(false);
      onChanged();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function logout() {
    const ok = await confirmDialog("登出 pCloud?");
    if (!ok) return;
    try {
      await api.post("/api/pcloud/logout");
      toast.success("已登出");
      onChanged();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  if (!status) return null;

  return (
    <div className="rounded-md border border-white/10 bg-white/5 px-4 py-3">
      {status.logged_in ? (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="text-emerald-300">●</span>
          <span className="text-white/90">
            {status.username || "(已登入)"}
          </span>
          <span className="rounded bg-white/10 px-2 py-0.5 text-xs font-mono text-white/60">
            {status.region.toUpperCase()}
          </span>
          <span className="text-white/40">
            預設目錄
            <span className="ml-1 font-mono text-white/70">
              {status.default_folder || "/"}
            </span>
          </span>
          <button onClick={logout} className="ml-auto btn-ghost text-xs">
            登出
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-amber-300">●</span>
            <span className="text-white/80">尚未登入 pCloud</span>
            {(status.has_env_credentials || status.has_env_token) && (
              <span className="text-xs text-white/40">
                (.env 已設定 — 首次呼叫 API 會自動登入)
              </span>
            )}
            <button
              className="ml-auto btn-ghost text-xs"
              onClick={() => setShowLogin((v) => !v)}
            >
              {showLogin ? "收起" : "用帳密/Token 登入"}
            </button>
          </div>

          {showLogin && (
            <form onSubmit={submit} className="space-y-2">
              <div className="flex gap-1">
                <button
                  type="button"
                  onClick={() => setMode("password")}
                  className={
                    mode === "password" ? "btn-primary text-xs" : "btn-ghost text-xs"
                  }
                >
                  帳密
                </button>
                <button
                  type="button"
                  onClick={() => setMode("token")}
                  className={
                    mode === "token" ? "btn-primary text-xs" : "btn-ghost text-xs"
                  }
                >
                  Access Token
                </button>
              </div>
              {mode === "password" ? (
                <div className="flex flex-wrap gap-2">
                  <input
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="pCloud Email"
                    className="flex-1 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
                    autoComplete="username"
                  />
                  <input
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="密碼"
                    type="password"
                    className="flex-1 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
                    autoComplete="current-password"
                  />
                </div>
              ) : (
                <input
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="貼上 pCloud access token"
                  className="w-full rounded-md border border-white/10 bg-panel px-2 py-1 font-mono text-sm outline-none focus:border-accent"
                />
              )}
              <button
                type="submit"
                className="btn-primary text-sm"
                disabled={submitting}
              >
                {submitting ? "登入中…" : "登入"}
              </button>
            </form>
          )}
        </div>
      )}
    </div>
  );
}

function QueueBar({
  queue,
  onCleanup,
}: {
  queue: PCloudQueueStatus | null;
  onCleanup: (keepFailed: boolean) => void;
}) {
  if (!queue) return null;
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-xs text-white/70">
      <span>排隊 {queue.pending}</span>
      <span className="text-white/40">|</span>
      <span>傳輸中 {queue.running}</span>
      <span className="text-white/40">|</span>
      <span className="text-emerald-300/80">完成 {queue.done}</span>
      <span className="text-white/40">|</span>
      <span className="text-red-300/80">失敗 {queue.failed}</span>
      <span className="text-white/40">|</span>
      <span className="text-white/50">
        併發上限 {queue.concurrency}・本機已送出 {queue.inflight}
      </span>
      <button
        onClick={() => onCleanup(true)}
        className="ml-auto rounded border border-white/10 px-2 py-0.5 text-white/70 hover:bg-white/10"
        title="清掉 已完成 + 已取消"
      >
        清掉完成
      </button>
      <button
        onClick={() => onCleanup(false)}
        className="rounded border border-red-500/30 px-2 py-0.5 text-red-300 hover:bg-red-500/10"
        title="清掉 已完成 + 已取消 + 失敗"
      >
        清掉所有結束項
      </button>
    </div>
  );
}

function TransfersTable({
  items,
  onRetry,
  onCancel,
}: {
  items: PCloudTransfer[];
  onRetry: (id: number) => void;
  onCancel: (id: number) => void;
}) {
  if (!items.length) {
    return (
      <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
        沒有傳輸任務
      </div>
    );
  }
  // Group by destination folder for visual grouping.
  const groups = useMemo(() => {
    const m = new Map<string, PCloudTransfer[]>();
    for (const it of items) {
      const k = it.pcloud_folder_path || "/";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(it);
    }
    return Array.from(m.entries());
  }, [items]);

  return (
    <div className="space-y-3">
      {groups.map(([path, rows]) => (
        <div key={path} className="overflow-hidden rounded-lg border border-white/10">
          <div className="flex items-center justify-between border-b border-white/10 bg-white/5 px-3 py-2 text-xs">
            <span className="font-mono text-white/70">{path}</span>
            <span className="text-white/40">{rows.length} 個檔案</span>
          </div>
          <table className="w-full text-sm">
            <thead className="bg-white/[0.02] text-left text-xs uppercase tracking-wide text-white/40">
              <tr>
                <th className="px-3 py-2">檔名</th>
                <th className="px-3 py-2 w-24">狀態</th>
                <th className="px-3 py-2 w-40">進度</th>
                <th className="px-3 py-2 w-24">大小</th>
                <th className="px-3 py-2 w-28">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const pct = r.pikpak_size
                  ? Math.min(100, Math.round((r.bytes_downloaded / r.pikpak_size) * 100))
                  : 0;
                return (
                  <tr key={r.id} className="border-t border-white/5 align-top">
                    <td className="px-3 py-2">
                      <div className="truncate text-white/90">
                        {r.pikpak_name || `(file_id ${r.pikpak_file_id})`}
                      </div>
                      {r.pikpak_path && (
                        <div className="text-xs text-white/40">
                          來源子路徑: {r.pikpak_path}
                        </div>
                      )}
                      {r.message && (
                        <div className="text-xs text-white/40">{r.message}</div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={
                          "rounded px-2 py-0.5 text-xs " +
                          (STATUS_COLORS[r.status] || "bg-white/10")
                        }
                      >
                        {r.status}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      {r.status === "running" && r.pikpak_size > 0 ? (
                        <div className="space-y-0.5">
                          <div className="h-1.5 w-full overflow-hidden rounded bg-white/10">
                            <div
                              className="h-full bg-accent"
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                          <div className="text-xs text-white/50">
                            {fmtBytes(r.bytes_downloaded)} / {fmtBytes(r.pikpak_size)}
                            ({pct}%)
                          </div>
                        </div>
                      ) : r.status === "done" ? (
                        <span className="text-xs text-emerald-300/80">100%</span>
                      ) : (
                        <span className="text-xs text-white/40">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-white/70">
                      {fmtBytes(r.pikpak_size)}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex gap-2 text-xs">
                        {(r.status === "failed" || r.status === "cancelled") && (
                          <button
                            onClick={() => onRetry(r.id)}
                            className="text-amber-300 hover:underline"
                          >
                            重試
                          </button>
                        )}
                        {(r.status === "pending" || r.status === "running") && (
                          <button
                            onClick={() => onCancel(r.id)}
                            className="text-red-300 hover:underline"
                          >
                            取消
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
