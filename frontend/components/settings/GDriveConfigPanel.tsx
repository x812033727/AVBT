"use client";

import { useEffect, useState } from "react";
import { Check, Copy, KeyRound, Pencil, Plus, Trash2, TriangleAlert } from "lucide-react";
import { confirmDialog } from "@/components/Toast";
import { api, type GDriveConfig, type GDriveOAuthClient } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import type { SetMsg } from "./types";

type Draft = { id: number | null; label: string; client_id: string; client_secret: string };

const EMPTY: Draft = { id: null, label: "", client_id: "", client_secret: "" };

// Google Drive 的 OAuth 基本設定:加密金鑰(存 data/ 檔案,不進 DB)、
// 重新導向網址、可多組的 OAuth 用戶端(密鑰加密後存 DB,畫面只顯示末四碼)。
export default function GDriveConfigPanel({
  config,
  reload,
  setMsg,
}: {
  config: GDriveConfig;
  reload: () => Promise<void>;
  setMsg: SetMsg;
}) {
  const [customKey, setCustomKey] = useState("");
  const [showKeyInput, setShowKeyInput] = useState(false);
  const [redirect, setRedirect] = useState(config.redirect_uri);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => setRedirect(config.redirect_uri), [config.redirect_uri]);

  const keyReady = config.key_configured;
  const keyFromEnv = config.key_source === "env";

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      setMsg({ kind: "ok", text: ok });
      await reload();
      return true;
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function saveKey(generate: boolean) {
    if (keyReady) {
      const ok = await confirmDialog(
        "更換加密金鑰?",
        "會用新金鑰重新加密所有 Drive 帳號 token 與 OAuth 用戶端密鑰。舊的自動備份將無法再用新金鑰解開。"
      );
      if (!ok) return;
    }
    const key = generate ? undefined : customKey.trim();
    if (!generate && !key) {
      setMsg({ kind: "err", text: "請貼上金鑰,或改按「自動產生」" });
      return;
    }
    const done = await run(
      () => api.post("/api/gdrive/config/token-key", { key }),
      keyReady ? "加密金鑰已更換,資料已重新加密" : "加密金鑰已設定"
    );
    if (done) {
      setCustomKey("");
      setShowKeyInput(false);
    }
  }

  async function saveRedirect() {
    await run(
      () => api.put("/api/gdrive/config/redirect-uri", { redirect_uri: redirect.trim() }),
      "重新導向網址已儲存"
    );
  }

  async function copyRedirect() {
    try {
      await navigator.clipboard.writeText(config.redirect_uri);
      setMsg({ kind: "ok", text: "已複製重新導向網址" });
    } catch {
      setMsg({ kind: "err", text: "複製失敗(瀏覽器可能擋下了)" });
    }
  }

  async function saveClient() {
    if (!draft) return;
    const body = {
      label: draft.label.trim(),
      client_id: draft.client_id.trim(),
      client_secret: draft.client_secret.trim(),
    };
    const done = await run(
      () =>
        draft.id === null
          ? api.post("/api/gdrive/clients", body)
          : api.patch(`/api/gdrive/clients/${draft.id}`, body),
      draft.id === null ? "OAuth 用戶端已新增" : "OAuth 用戶端已更新"
    );
    if (done) setDraft(null);
  }

  async function toggleClient(c: GDriveOAuthClient, enabled: boolean) {
    await run(() => api.patch(`/api/gdrive/clients/${c.id}`, { enabled }), enabled ? "已啟用" : "已停用");
  }

  async function deleteClient(c: GDriveOAuthClient) {
    const ok = await confirmDialog(`刪除 OAuth 用戶端「${c.label}」?`, "只會刪除本站的設定,不會動到 Google Cloud。");
    if (!ok) return;
    await run(() => api.del(`/api/gdrive/clients/${c.id}`), "OAuth 用戶端已刪除");
  }

  return (
    <div className="space-y-4">
      {/* ---- 1. 加密金鑰 ---- */}
      <div className="space-y-2">
        <h3 className="inline-flex items-center gap-1.5 text-sm font-medium">
          <KeyRound className="h-4 w-4" aria-hidden /> 加密金鑰
        </h3>
        <div className="text-sm">
          {config.key_source === "none" && <span className="text-amber-300">尚未設定(請先設定才能新增用戶端與帳號)</span>}
          {config.key_source === "file" && (
            <span className="inline-flex items-center gap-1 text-emerald-300">
              <Check className="h-3.5 w-3.5" aria-hidden /> 已設定・存於伺服器 data/gdrive_token_key.txt(不在資料庫)
            </span>
          )}
          {keyFromEnv && (
            <span className="inline-flex items-center gap-1 text-emerald-300">
              <Check className="h-3.5 w-3.5" aria-hidden /> 已設定・來自 .env 的 GDRIVE_TOKEN_KEY(要更換請改 .env)
            </span>
          )}
        </div>
        {!keyFromEnv && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant={keyReady ? "outline" : "default"} disabled={busy} onClick={() => saveKey(true)}>
              {keyReady ? "更換為新的隨機金鑰" : "自動產生金鑰"}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setShowKeyInput((v) => !v)}>
              {showKeyInput ? "取消" : "填入自訂金鑰"}
            </Button>
            {showKeyInput && (
              <>
                <Input
                  className="w-96 max-w-full font-mono"
                  type="password"
                  placeholder="44 字元 Fernet 金鑰"
                  value={customKey}
                  onChange={(e) => setCustomKey(e.target.value)}
                />
                <Button size="sm" disabled={busy} onClick={() => saveKey(false)}>
                  儲存
                </Button>
              </>
            )}
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          用來加密資料庫裡的 Drive token 與用戶端密鑰;資料庫外流也解不開。金鑰檔會跟著自動備份到 data/backups。
        </p>
      </div>

      {/* ---- 2. 重新導向網址 ---- */}
      <div className="space-y-2">
        <h3 className="text-sm font-medium">重新導向網址</h3>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            className="w-[30rem] max-w-full font-mono text-xs"
            value={redirect}
            disabled={config.redirect_source === "env"}
            onChange={(e) => setRedirect(e.target.value)}
          />
          {config.redirect_source !== "env" && (
            <Button size="sm" variant="outline" disabled={busy || redirect.trim() === config.redirect_uri} onClick={saveRedirect}>
              儲存
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={copyRedirect}>
            <Copy className="h-3.5 w-3.5" aria-hidden /> 複製
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          填後端對外的網址 + <span className="font-mono">/api/gdrive/oauth/callback</span>
          (localhost 以外必須是 https)。每組 OAuth 用戶端都要在 Google Cloud 登記這同一個網址。
          {config.redirect_source === "env" && " 目前來自 .env 的 GDRIVE_REDIRECT_URI。"}
          {config.redirect_source === "default" && " 目前是預設值,部署到伺服器時請改成實際網址。"}
        </p>
      </div>

      {/* ---- 3. OAuth 用戶端 ---- */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium">OAuth 用戶端</h3>
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={!keyReady || busy}
            onClick={() => setDraft({ ...EMPTY })}
          >
            <Plus className="h-3.5 w-3.5" aria-hidden /> 新增用戶端
          </Button>
        </div>
        {config.clients.length === 0 && !draft && (
          <p className="text-sm text-muted-foreground">還沒有 OAuth 用戶端。</p>
        )}
        {config.clients.map((c) => (
          <div key={c.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border p-2 text-sm">
            <span className="font-medium">{c.label}</span>
            <span className="max-w-[22rem] truncate font-mono text-xs text-muted-foreground" title={c.client_id}>
              {c.client_id}
            </span>
            <span className="font-mono text-xs text-muted-foreground">密鑰 {c.client_secret_masked}</span>
            {!c.secret_ok && (
              <span className="inline-flex items-center gap-1 text-xs text-amber-300">
                <TriangleAlert className="h-3 w-3" aria-hidden /> 密鑰無法解密,請重新填寫
              </span>
            )}
            <span className="text-xs text-muted-foreground">{c.account_count} 個帳號</span>
            <div className="ml-auto flex items-center gap-2">
              <Switch checked={c.enabled} onCheckedChange={(v) => toggleClient(c, v)} aria-label="啟用" />
              <Button
                size="sm"
                variant="outline"
                onClick={() => setDraft({ id: c.id, label: c.label, client_id: c.client_id, client_secret: "" })}
              >
                <Pencil className="h-3.5 w-3.5" aria-hidden /> 編輯
              </Button>
              <Button size="sm" variant="outline" onClick={() => deleteClient(c)} aria-label="刪除">
                <Trash2 className="h-3.5 w-3.5" aria-hidden />
              </Button>
            </div>
          </div>
        ))}
        {draft && (
          <div className="space-y-2 rounded-md border border-primary/40 p-3">
            <div className="text-sm font-medium">{draft.id === null ? "新增 OAuth 用戶端" : "編輯 OAuth 用戶端"}</div>
            <div className="grid gap-2 sm:grid-cols-3">
              <Input
                placeholder="名稱(例如:專案一)"
                value={draft.label}
                onChange={(e) => setDraft({ ...draft, label: e.target.value })}
              />
              <Input
                className="font-mono text-xs"
                placeholder="用戶端 ID(….apps.googleusercontent.com)"
                value={draft.client_id}
                onChange={(e) => setDraft({ ...draft, client_id: e.target.value })}
              />
              <Input
                className="font-mono text-xs"
                type="password"
                autoComplete="new-password"
                placeholder={draft.id === null ? "用戶端密鑰" : "用戶端密鑰(留空 = 不變更)"}
                value={draft.client_secret}
                onChange={(e) => setDraft({ ...draft, client_secret: e.target.value })}
              />
            </div>
            <div className="flex gap-2">
              <Button size="sm" disabled={busy} onClick={saveClient}>
                儲存
              </Button>
              <Button size="sm" variant="outline" onClick={() => setDraft(null)}>
                取消
              </Button>
            </div>
          </div>
        )}
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">如何在 Google Cloud 建立用戶端?</summary>
          <ol className="mt-1 list-decimal space-y-0.5 pl-5">
            <li>console.cloud.google.com 建立或選一個專案。</li>
            <li>API 和服務 → 程式庫 → 搜尋「Google Drive API」→ 啟用。</li>
            <li>Google Auth Platform → 品牌填名稱與 email;目標對象選「外部」並按「發布應用程式」(不發布 7 天後授權失效)。</li>
            <li>用戶端 → 建立用戶端 → 類型「網頁應用程式」→ 已授權的重新導向 URI 填上方網址。</li>
            <li>把拿到的用戶端 ID 與密鑰填到這裡。要用多個專案就重複以上步驟、新增多組。</li>
          </ol>
        </details>
      </div>
    </div>
  );
}
