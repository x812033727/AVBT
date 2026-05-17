"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { confirmDialog } from "@/components/Toast";
import {
  API_BASE,
  api,
  streamNdjson,
  type ArchiverStatus,
  type PikPakStatus,
  type PresenceDetail,
  type PresenceStatus,
  type TrackerStatus,
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

function fmt(d: string | null): string {
  if (!d) return "從未執行";
  return new Date(d.endsWith("Z") ? d : d + "Z").toLocaleString();
}

export default function SettingsPage() {
  const [pikpak, setPikpak] = useState<PikPakStatus | null>(null);
  const [archiver, setArchiver] = useState<ArchiverStatus | null>(null);
  const [tracker, setTracker] = useState<TrackerStatus | null>(null);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [tokenInput, setTokenInput] = useState("");
  const [storedToken, setStoredToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<
    { kind: "ok" | "err"; text: string } | null
  >(null);

  const loadAll = useCallback(async () => {
    const [p, a, t, tk] = await Promise.all([
      api.get<PikPakStatus>("/api/pikpak/status").catch(() => null),
      api.get<ArchiverStatus>("/api/pikpak/archiver").catch(() => null),
      api.get<TrackerStatus>("/api/tracked/status").catch(() => null),
      api.get<{ token: string }>("/api/pikpak/token").catch(() => null),
    ]);
    setPikpak(p);
    setArchiver(a);
    setTracker(t);
    setStoredToken(tk?.token || "");
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  async function login() {
    if (!username || !password) {
      setMsg({ kind: "err", text: "請填入帳號與密碼" });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.post("/api/pikpak/login", { username, password, remember: true });
      setMsg({ kind: "ok", text: `已登入並儲存 token：${username}` });
      setPassword("");
      await loadAll();
    } catch (e: any) {
      setMsg({ kind: "err", text: `登入失敗：${e.message}` });
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    const ok = await confirmDialog("登出並清除 token？");
    if (!ok) return;
    setBusy(true);
    try {
      await api.post("/api/pikpak/logout");
      setMsg({ kind: "ok", text: "已登出並刪除 token" });
      await loadAll();
    } finally {
      setBusy(false);
    }
  }

  async function loginWithToken() {
    if (!tokenInput.trim()) {
      setMsg({ kind: "err", text: "請貼上 Token" });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.post("/api/pikpak/login", {
        encoded_token: tokenInput.trim(),
        remember: true,
      });
      setTokenInput("");
      setMsg({ kind: "ok", text: "Token 已驗證並儲存" });
      await loadAll();
    } catch (e: any) {
      setMsg({ kind: "err", text: `Token 登入失敗：${e.message}` });
    } finally {
      setBusy(false);
    }
  }

  async function copyToken() {
    if (!storedToken) return;
    try {
      await navigator.clipboard.writeText(storedToken);
      setMsg({ kind: "ok", text: "Token 已複製到剪貼簿" });
    } catch {
      setMsg({ kind: "err", text: "複製失敗（瀏覽器可能擋下了）" });
    }
  }

  async function toggleArchiver(enabled: boolean) {
    const a = await api.post<ArchiverStatus>("/api/pikpak/archiver/toggle", {
      enabled,
    });
    setArchiver(a);
  }

  async function runArchiverNow() {
    setBusy(true);
    try {
      const a = await api.post<ArchiverStatus & { moved: number }>(
        "/api/pikpak/archiver/run"
      );
      setArchiver(a);
      setMsg({ kind: "ok", text: `歸檔執行完畢，這次搬了 ${a.moved} 個` });
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  async function toggleTracker(enabled: boolean) {
    const t = await api.post<TrackerStatus>("/api/tracked/status/toggle", {
      enabled,
    });
    setTracker(t);
  }

  async function runTrackerNow() {
    setBusy(true);
    try {
      const t = await api.post<TrackerStatus & { new_total: number }>(
        "/api/tracked/status/run-now"
      );
      setTracker(t);
      setMsg({ kind: "ok", text: `追蹤檢查完畢，這次找到 ${t.new_total} 部新作品` });
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {msg && (
        <div
          className={
            "rounded-md border px-3 py-2 text-sm " +
            (msg.kind === "ok"
              ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
              : "border-red-500/30 bg-red-500/10 text-red-300")
          }
        >
          {msg.text}
        </div>
      )}

      <section className="space-y-3 rounded-lg border border-white/10 bg-panel p-4">
        <h2 className="text-lg font-semibold">PikPak 帳號</h2>
        <div className="space-y-1 text-sm">
          <div>
            狀態：
            {pikpak?.logged_in ? (
              <span className="text-emerald-300">
                ✓ 已登入 {pikpak.username && `(${pikpak.username})`}
              </span>
            ) : (
              <span className="text-amber-300">未登入</span>
            )}
          </div>
          {pikpak?.quota && (
            <div className="text-xs text-white/60">
              空間：已用 {fmtBytes(pikpak.quota.used)} /{" "}
              {fmtBytes(pikpak.quota.limit)}
            </div>
          )}
          {pikpak?.quota_error && (
            <div className="text-xs text-amber-300/80">
              ⚠ 配額查詢失敗：{pikpak.quota_error}
            </div>
          )}
          <div className="text-xs text-white/40">
            Token 檔案：{pikpak?.has_stored_token ? "存在" : "無"} ・ .env 預設：
            {pikpak?.has_env_credentials ? "有" : "無"}
          </div>
        </div>

        <div className="grid gap-2 sm:grid-cols-2">
          <input
            type="email"
            placeholder="username / email"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="rounded-md border border-white/10 bg-ink px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <input
            type="password"
            placeholder="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="rounded-md border border-white/10 bg-ink px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </div>
        <div className="flex gap-2">
          <button
            className="btn-primary disabled:opacity-50"
            onClick={login}
            disabled={busy}
          >
            {busy ? "登入中…" : "登入並儲存"}
          </button>
          {pikpak?.logged_in && (
            <button className="btn-ghost" onClick={logout} disabled={busy}>
              登出
            </button>
          )}
        </div>
        <p className="text-xs text-white/40">
          帳密只用來換取 token，token 存在 <span className="font-mono">data/pikpak_token.txt</span>
          ，重啟後自動載入。
        </p>

        <div className="border-t border-white/10 pt-3">
          <h3 className="text-sm font-semibold text-white/80">
            或直接貼 Token 登入
          </h3>
          <p className="text-xs text-white/40">
            如果你從其他 PikPak 工具取得 encoded_token，可以直接貼進來免再次輸入帳密。
          </p>
          <textarea
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            placeholder="貼上 encoded_token …"
            rows={3}
            className="mt-2 w-full rounded-md border border-white/10 bg-ink px-3 py-2 text-xs font-mono outline-none focus:border-accent"
          />
          <div className="mt-2 flex gap-2">
            <button
              className="btn-primary disabled:opacity-50"
              onClick={loginWithToken}
              disabled={busy || !tokenInput.trim()}
            >
              使用此 Token
            </button>
            {storedToken && (
              <>
                <button
                  className="btn-ghost"
                  onClick={() => setShowToken((s) => !s)}
                >
                  {showToken ? "隱藏目前 Token" : "顯示目前 Token"}
                </button>
                <button className="btn-ghost" onClick={copyToken}>
                  複製目前 Token
                </button>
              </>
            )}
          </div>
          {showToken && storedToken && (
            <textarea
              readOnly
              value={storedToken}
              rows={3}
              className="mt-2 w-full rounded-md border border-white/10 bg-ink/50 px-3 py-2 text-xs font-mono text-white/60 outline-none"
              onFocus={(e) => e.target.select()}
            />
          )}
        </div>
      </section>

      <section className="space-y-3 rounded-lg border border-white/10 bg-panel p-4">
        <h2 className="text-lg font-semibold">自動歸檔</h2>
        {archiver ? (
          <>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={archiver.enabled}
                onChange={(e) => toggleArchiver(e.target.checked)}
              />
              啟用（每 {archiver.interval_seconds} 秒掃一次）
            </label>
            <div className="text-xs text-white/60">
              路徑：
              <span className="font-mono">{archiver.archive_folder}/&lt;番號&gt;</span>
            </div>
            <div className="text-xs text-white/60">
              累計歸檔：{archiver.archived_total} ・ 最後執行 {fmt(archiver.last_run)}
            </div>
            {archiver.last_error && (
              <div className="text-xs text-amber-300/80">⚠ {archiver.last_error}</div>
            )}
            <button
              className="btn-ghost"
              onClick={runArchiverNow}
              disabled={busy}
            >
              立即執行
            </button>
          </>
        ) : (
          <div className="text-sm text-white/40">載入中…</div>
        )}
      </section>

      <section className="space-y-3 rounded-lg border border-white/10 bg-panel p-4">
        <h2 className="text-lg font-semibold">女優追蹤</h2>
        {tracker ? (
          <>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={tracker.enabled}
                onChange={(e) => toggleTracker(e.target.checked)}
              />
              啟用（每 {tracker.interval_seconds} 秒掃一次）
            </label>
            <div className="text-xs text-white/60">
              最後執行 {fmt(tracker.last_run)} ・ 上次找到 {tracker.last_new_total} 部新作品
            </div>
            {tracker.last_error && (
              <div className="text-xs text-amber-300/80">⚠ {tracker.last_error}</div>
            )}
            <button className="btn-ghost" onClick={runTrackerNow} disabled={busy}>
              立即執行
            </button>
          </>
        ) : (
          <div className="text-sm text-white/40">載入中…</div>
        )}
      </section>

      <ReorganizeSection setMsg={setMsg} />

      <BackupSection setMsg={setMsg} />

      <section className="space-y-1 rounded-lg border border-white/10 bg-panel p-4 text-xs text-white/60">
        <h2 className="text-sm font-semibold text-white/80">其他設定（環境變數）</h2>
        <p>
          以下設定必須在 <span className="font-mono">backend/.env</span> 修改後重啟：
        </p>
        <ul className="list-inside list-disc pl-2">
          <li>JAVBUS_BASE_URL：JavBus 站台網址（被擋時可換鏡像）</li>
          <li>HTTP_PROXY：HTTP/SOCKS 代理</li>
          <li>WEBHOOK_URL：歸檔 / 新作品事件的 webhook</li>
          <li>PIKPAK_DOWNLOAD_FOLDER：離線下載預設資料夾</li>
        </ul>
      </section>
    </div>
  );
}

type ReorgProgress = {
  current: number;
  source: string;
  kind: "folder" | "file";
  action: "move" | "rename" | "flatten" | "dedupe" | "skip" | "error";
  target: string | null;
  reason: string | null;
  section?: "migrate" | "cleanup";
  context?: string;
};

type ReorgResult = {
  total: number;
  moved: number;
  renamed: number;
  flattened: number;
  deduped: number;
  skipped: number;
  errors: number;
  dry_run: boolean;
};

const REORG_ACTION: Record<ReorgProgress["action"], { text: string; cls: string }> = {
  move: { text: "→ 搬移", cls: "text-blue-300" },
  rename: { text: "✎ 改名", cls: "text-cyan-300" },
  flatten: { text: "📤 攤平", cls: "text-emerald-300" },
  dedupe: { text: "🗑 去重", cls: "text-purple-300" },
  skip: { text: "⏭ 略過", cls: "text-white/50" },
  error: { text: "✗ 失敗", cls: "text-red-300" },
};

const REORG_REASON: Record<string, string> = {
  no_code: "無法辨識番號",
  no_tracked_match: "沒有對應的追蹤分類",
  conflict: "目標已有同名資料夾",
  bad_target: "解析目標路徑失敗",
  resolve_failed: "查詢 JavBus 失敗",
  already_clean: "已經規範化",
  duplicate: "重複（保留較大者）",
};

function ReorganizeSection({
  setMsg,
}: {
  setMsg: (m: { kind: "ok" | "err"; text: string } | null) => void;
}) {
  const [presence, setPresence] = useState<PresenceDetail | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showDebug, setShowDebug] = useState(false);
  const [open, setOpen] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [busy, setBusy] = useState(false);
  const [total, setTotal] = useState(0);
  const [progress, setProgress] = useState<ReorgProgress[]>([]);
  const [result, setResult] = useState<ReorgResult | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [cleanupTargets, setCleanupTargets] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const loadPresence = useCallback(async () => {
    try {
      const p = await api.get<PresenceDetail>("/api/pikpak/presence/detail");
      setPresence(p);
    } catch {
      setPresence(null);
    }
  }, []);

  useEffect(() => {
    loadPresence();
  }, [loadPresence]);

  async function refreshIndex() {
    setRefreshing(true);
    try {
      await api.post<PresenceStatus>("/api/pikpak/presence/refresh");
      const p = await api.get<PresenceDetail>(
        "/api/pikpak/presence/detail"
      );
      setPresence(p);
      setMsg({
        kind: "ok",
        text: `PikPak 索引已重建（共 ${p.size} 個番號）`,
      });
    } catch (e: any) {
      setMsg({ kind: "err", text: `重建索引失敗：${e.message}` });
    } finally {
      setRefreshing(false);
    }
  }

  async function runReorg() {
    if (!dryRun) {
      if (
        !confirm(
          "正式整理會把 AVBT/已完成 下的番號資料夾搬到對應的 series / star 子資料夾。確定？"
        )
      )
        return;
    }
    setBusy(true);
    setErrMsg(null);
    setResult(null);
    setProgress([]);
    setTotal(0);
    setCleanupTargets([]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const wasDryRun = dryRun;
    try {
      await streamNdjson(
        "/api/pikpak/reorganize",
        { dry_run: wasDryRun },
        (event) => {
          if (event.type === "start") {
            setTotal(event.total ?? 0);
            setCleanupTargets(event.cleanup_targets ?? []);
          }
          else if (event.type === "progress")
            setProgress((prev) => [...prev, event]);
          else if (event.type === "done") setResult(event.result);
          else if (event.type === "error") setErrMsg(event.message);
        },
        ctrl.signal
      );
      if (!wasDryRun) {
        loadPresence();
      }
    } catch (e: any) {
      if (e.name !== "AbortError") setErrMsg(e.message);
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  function close() {
    if (busy) return;
    setOpen(false);
    setProgress([]);
    setResult(null);
    setErrMsg(null);
    setTotal(0);
    setDryRun(true);
  }

  const percent = total ? Math.round((progress.length / total) * 100) : 0;
  const recent = progress.slice(-10).reverse();

  return (
    <section className="space-y-3 rounded-lg border border-white/10 bg-panel p-4">
      <h2 className="text-lg font-semibold">PikPak 資料夾結構整理</h2>
      <p className="text-xs text-white/50">
        新下載會自動依追蹤的系列 / 女優分類，歸檔到{" "}
        <span className="font-mono">AVBT/&lt;類別&gt;/&lt;名稱&gt;/&lt;番號&gt;</span>
        。下方可重建索引、或把舊的扁平歸檔搬到新結構。
      </p>
      {presence ? (
        <div className="space-y-1 text-xs text-white/60">
          <div>
            索引狀態：{presence.ready ? (
              <span className="text-emerald-300">已建立</span>
            ) : (
              <span className="text-amber-300">尚未建立</span>
            )}
            ・收錄 <span className="font-mono">{presence.size}</span> 個番號
          </div>
          <div>
            最後建立：
            {presence.built_at
              ? new Date(presence.built_at + "Z").toLocaleString()
              : "從未"}
            ・TTL {presence.ttl_seconds}s
          </div>
          {presence.last_error && (
            <div className="text-amber-300/80">⚠ {presence.last_error}</div>
          )}
        </div>
      ) : (
        <div className="text-sm text-white/40">載入中…</div>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          className="btn-ghost"
          onClick={refreshIndex}
          disabled={refreshing}
        >
          {refreshing ? "重建索引中…" : "重建索引"}
        </button>
        <button className="btn-ghost" onClick={() => setOpen(true)}>
          整理舊資料夾…
        </button>
        <button
          className="btn-ghost"
          onClick={() => setShowDebug((v) => !v)}
        >
          {showDebug ? "收合偵錯" : "看索引偵錯"}
        </button>
      </div>

      {showDebug && presence && (
        <div className="space-y-2 rounded-md border border-white/10 bg-ink/40 p-3 text-xs">
          <div className="font-semibold text-white/70">
            掃描的根目錄(共 {presence.roots.length})
          </div>
          {presence.roots.length === 0 ? (
            <div className="text-white/40">
              索引還沒建立過。請點上方「重建索引」。
            </div>
          ) : (
            <ul className="space-y-0.5 font-mono">
              {presence.roots.map((r) => (
                <li key={r.path} className="flex flex-wrap gap-2">
                  <span className="text-white/80">{r.path}</span>
                  <span className="text-white/40">
                    leaves={r.leaves} · codes={r.codes}
                    {r.unrecognized > 0 && (
                      <span className="text-amber-300">
                        {" · ⚠ unrecognized=" + r.unrecognized}
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className="pt-1 font-semibold text-white/70">
            無法辨識為番號的資料夾名(共 {presence.unrecognized_total}
            ,最多顯示 50 個)
          </div>
          {presence.unrecognized.length === 0 ? (
            <div className="text-white/40">
              ✓ 所有掃到的葉節點都成功辨識為番號。
            </div>
          ) : (
            <ul className="max-h-64 space-y-0.5 overflow-auto font-mono">
              {presence.unrecognized.map((u, i) => (
                <li key={i} className="text-amber-200/80">
                  <span className="text-white/40">{u.parent}/</span>
                  {u.name}
                </li>
              ))}
            </ul>
          )}
          <div className="pt-1 text-white/40">
            說明:索引只會掃 <span className="font-mono">AVBT/star</span>、
            <span className="font-mono">/series</span>、
            <span className="font-mono">/studio</span>、
            <span className="font-mono">/label</span>、
            <span className="font-mono">/director</span> 下的
            <span className="font-mono">&lt;name&gt;/&lt;code&gt;</span>
            ,加上舊版 <span className="font-mono">AVBT/已完成/&lt;code&gt;</span>
            。若你的檔案在其他路徑(例如直接放在 AVBT/),會被當成「找不到」。
          </div>
        </div>
      )}

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
          onClick={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div className="w-full max-w-xl space-y-4 rounded-xl border border-white/10 bg-panel p-5">
            <div className="flex items-center">
              <h2 className="text-lg font-semibold">重新整理 PikPak 結構</h2>
              <button
                className="ml-auto text-white/40 hover:text-white"
                onClick={close}
              >
                ✕
              </button>
            </div>

            <div className="space-y-1 text-xs text-white/50">
              <p>
                <span className="rounded bg-blue-500/15 px-1 text-[10px] text-blue-300">
                  搬移
                </span>{" "}
                把{" "}
                <span className="font-mono">AVBT/已完成</span> 下的番號搬到對應追蹤分類（優先序 series &gt; director &gt; label &gt; studio &gt; star）
                <span className="font-mono">
                  AVBT/&lt;類別&gt;/&lt;名稱&gt;/&lt;番號&gt;
                </span>
                。
              </p>
              <p>
                <span className="rounded bg-purple-500/15 px-1 text-[10px] text-purple-300">
                  清理
                </span>{" "}
                走訪每個追蹤分類的目的資料夾：髒名字改成 canonical
                <span className="font-mono">（&lt;番號&gt; / &lt;番號&gt;.ext）</span>
                ；wrapper 資料夾裡有 ≥1 個主檔（≥300 MB）就攤平，取最大的影片到父層、其餘垃圾與空殼丟回收筒；同番號重複時保留較大者。
              </p>
            </div>

            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
                disabled={busy}
              />
              <span>只預覽（不實際搬移）</span>
            </label>

            {cleanupTargets.length > 0 && (
              <details className="rounded-md border border-white/10 bg-ink/40 px-3 py-2 text-xs">
                <summary className="cursor-pointer text-white/70">
                  清理階段會掃 {cleanupTargets.length} 個資料夾
                </summary>
                <ul className="mt-2 max-h-40 space-y-0.5 overflow-y-auto">
                  {cleanupTargets.map((p) => (
                    <li key={p} className="truncate font-mono text-white/50">
                      {p}
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-white/40">
                  路徑不對？檢查 .env 的{" "}
                  <span className="font-mono">PIKPAK_{"<KIND>"}_FOLDER</span>{" "}
                  設定（例如{" "}
                  <span className="font-mono">PIKPAK_SERIES_FOLDER</span>），重啟後重試。
                </p>
              </details>
            )}

            {errMsg && (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {errMsg}
              </div>
            )}

            {(busy || result) && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs text-white/60">
                  <span>
                    {progress.length} / {total} ({percent}%)
                    {result?.dry_run && " ・ 預覽模式"}
                  </span>
                  <span>
                    搬 {progress.filter((p) => p.action === "move").length} ／
                    名 {progress.filter((p) => p.action === "rename").length} ／
                    平 {progress.filter((p) => p.action === "flatten").length} ／
                    去 {progress.filter((p) => p.action === "dedupe").length} ／
                    略 {progress.filter((p) => p.action === "skip").length} ／
                    錯 {progress.filter((p) => p.action === "error").length}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded bg-white/10">
                  <div
                    className="h-full bg-accent transition-[width]"
                    style={{ width: `${percent}%` }}
                  />
                </div>
                <ul className="max-h-56 overflow-y-auto rounded-md border border-white/10 bg-ink/50 p-2 text-xs">
                  {recent.length === 0 && (
                    <li className="text-white/40">等待第一筆…</li>
                  )}
                  {recent.map((p) => {
                    const lbl = REORG_ACTION[p.action];
                    const reasonTxt =
                      p.reason && REORG_REASON[p.reason]
                        ? `（${REORG_REASON[p.reason]}）`
                        : p.reason
                        ? `（${p.reason}）`
                        : "";
                    const icon = p.kind === "file" ? "📄" : "📁";
                    const sectionTag =
                      p.section === "cleanup" ? (
                        <span className="rounded bg-purple-500/15 px-1 text-[10px] text-purple-300">
                          清理
                        </span>
                      ) : p.section === "migrate" ? (
                        <span className="rounded bg-blue-500/15 px-1 text-[10px] text-blue-300">
                          搬移
                        </span>
                      ) : null;
                    return (
                      <li
                        key={`${p.current}-${p.source}`}
                        className="flex items-baseline gap-2 py-0.5"
                      >
                        {sectionTag}
                        <span className={lbl.cls}>
                          {lbl.text}
                          {reasonTxt}
                        </span>
                        <span className="truncate text-white/60">
                          {icon} {p.source}
                        </span>
                        {p.target && (
                          <>
                            <span className="text-white/30">→</span>
                            <span className="truncate font-mono text-accent">
                              {p.target}
                            </span>
                          </>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {result && (
              <div className="space-y-1 rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm">
                <div>
                  共 <strong>{result.total}</strong> 個項目
                  {result.dry_run && (
                    <span className="ml-2 text-amber-300/80">
                      （僅預覽，未修改）
                    </span>
                  )}
                </div>
                <div className="text-blue-300">→ 搬移 {result.moved}</div>
                <div className="text-cyan-300">✎ 改名 {result.renamed}</div>
                <div className="text-emerald-300">📤 攤平 {result.flattened}</div>
                <div className="text-purple-300">🗑 去重 {result.deduped}</div>
                <div className="text-white/60">⏭ 略過 {result.skipped}</div>
                {result.errors > 0 && (
                  <div className="text-red-300">✗ 失敗 {result.errors}</div>
                )}
              </div>
            )}

            <div className="flex justify-end gap-2">
              {busy ? (
                <button
                  className="btn-ghost"
                  onClick={() => abortRef.current?.abort()}
                >
                  取消
                </button>
              ) : (
                <>
                  <button className="btn-ghost" onClick={close}>
                    關閉
                  </button>
                  <button className="btn-primary" onClick={runReorg}>
                    {dryRun ? "預覽" : "執行"}
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}


function BackupSection({
  setMsg,
}: {
  setMsg: (m: { kind: "ok" | "err"; text: string } | null) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [overwrite, setOverwrite] = useState(false);

  function download() {
    const url = `${API_BASE}/api/backup`;
    const a = document.createElement("a");
    a.href = url;
    a.click();
  }

  async function upload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setMsg(null);
    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const res = await api.post<{ stats: any }>(
        `/api/backup/restore?overwrite=${overwrite}`,
        payload
      );
      const s = res.stats;
      setMsg({
        kind: "ok",
        text:
          `匯入完成 — ` +
          `收藏 ${s.collection.added}新 / ${s.collection.updated}改 / ${s.collection.skipped}略, ` +
          `追蹤 ${s.tracked.added}新 / ${s.tracked.updated}改 / ${s.tracked.skipped}略, ` +
          `紀錄 ${s.history.added}新 / ${s.history.skipped}略`,
      });
    } catch (e: any) {
      setMsg({ kind: "err", text: `匯入失敗：${e.message}` });
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-white/10 bg-panel p-4">
      <h2 className="text-lg font-semibold">備份 / 還原</h2>
      <p className="text-xs text-white/50">
        匯出包含：收藏清單、追蹤的女優、所有送出紀錄。不含 PikPak token 與設定。
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-ghost" onClick={download} disabled={busy}>
          下載備份 (JSON)
        </button>
        <button
          className="btn-ghost"
          onClick={() => fileRef.current?.click()}
          disabled={busy}
        >
          選擇備份檔還原…
        </button>
        <label className="flex items-center gap-1 text-xs text-white/60">
          <input
            type="checkbox"
            checked={overwrite}
            onChange={(e) => setOverwrite(e.target.checked)}
          />
          覆蓋現有
        </label>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={upload}
        />
      </div>
    </section>
  );
}
