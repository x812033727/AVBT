"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  API_BASE,
  api,
  type ArchiverStatus,
  type PikPakStatus,
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
    if (!confirm("登出並清除 token？")) return;
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
