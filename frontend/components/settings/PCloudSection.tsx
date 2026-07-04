"use client";

import { useCallback, useEffect, useState } from "react";
import { confirmDialog } from "@/components/Toast";
import { api, type PCloudStatus } from "@/lib/api";
import { fmtBytes } from "./shared";

export default function PCloudSection({
  setMsg,
}: {
  setMsg: (m: { kind: "ok" | "err"; text: string } | null) => void;
}) {
  const [status, setStatus] = useState<PCloudStatus | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const s = await api
      .get<PCloudStatus>("/api/pcloud/status")
      .catch(() => null);
    setStatus(s);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function login() {
    if (!username || !password) {
      setMsg({ kind: "err", text: "請填入 pCloud 帳號與密碼" });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const res = await api.post<{ host: string; username: string }>(
        "/api/pcloud/login",
        { username, password, remember: true }
      );
      setMsg({
        kind: "ok",
        text: `pCloud 已登入：${res.username}（${res.host}）`,
      });
      setPassword("");
      await load();
    } catch (e: any) {
      setMsg({ kind: "err", text: `pCloud 登入失敗：${e.message}` });
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    const ok = await confirmDialog("登出 pCloud 並清除 token？");
    if (!ok) return;
    setBusy(true);
    try {
      await api.post("/api/pcloud/logout");
      setMsg({ kind: "ok", text: "pCloud 已登出" });
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-white/10 bg-panel p-4">
      <h2 className="text-lg font-semibold">pCloud 帳號</h2>
      <div className="space-y-1 text-sm">
        <div>
          狀態：
          {status?.logged_in ? (
            <span className="text-emerald-300">
              ✓ 已登入 {status.username && `(${status.username})`}
            </span>
          ) : (
            <span className="text-amber-300">未登入</span>
          )}
        </div>
        {status?.logged_in && status.host && (
          <div className="text-xs text-white/50">
            資料中心：<span className="font-mono">{status.host}</span>
          </div>
        )}
        {status?.quota && (
          <div className="text-xs text-white/60">
            空間：已用 {fmtBytes(status.quota.used)} /{" "}
            {fmtBytes(status.quota.limit)}
          </div>
        )}
        {status?.quota_error && (
          <div className="text-xs text-amber-300/80">
            ⚠ 配額查詢失敗：{status.quota_error}
          </div>
        )}
        <div className="text-xs text-white/40">
          Token 檔案：{status?.has_stored_token ? "存在" : "無"} ・ .env 預設：
          {status?.has_env_credentials ? "有" : "無"}
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
        {status?.logged_in && (
          <button className="btn-ghost" onClick={logout} disabled={busy}>
            登出
          </button>
        )}
      </div>
      <p className="text-xs text-white/40">
        pCloud 有美國 / 歐洲兩個資料中心，會自動偵測。Token 存在{" "}
        <span className="font-mono">data/pcloud_token.json</span>
        ，重啟後自動載入。
      </p>
    </section>
  );
}
