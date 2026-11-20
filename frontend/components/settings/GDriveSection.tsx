"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, RefreshCw, TriangleAlert } from "lucide-react";
import { confirmDialog } from "@/components/Toast";
import { api, type GDriveAccount, type GDrivePolicy, type GDriveStatus } from "@/lib/api";
import { fmtBytes, fmtDateTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import type { SetMsg } from "./types";

// Google Drive 帳號(OAuth)+ 流量控管。帳號 token 由後端加密保存,
// 這裡只看得到 email / 配額 / 今日上傳量。
export default function GDriveSection({ setMsg }: { setMsg: SetMsg }) {
  const [status, setStatus] = useState<GDriveStatus | null>(null);
  const [newLabel, setNewLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [budget, setBudget] = useState("");
  const [windowStr, setWindowStr] = useState("");

  const load = useCallback(async () => {
    const s = await api.get<GDriveStatus>("/api/gdrive/status").catch(() => null);
    setStatus(s);
    if (s) {
      setBudget(String(s.queue.policy.daily_budget_gb || 0));
      setWindowStr(s.queue.policy.window || "");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // OAuth 完成後後端把瀏覽器導回 /settings?gdrive=ok|error&msg=…
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const result = q.get("gdrive");
    if (!result) return;
    const text = q.get("msg") || "";
    setMsg(
      result === "ok"
        ? { kind: "ok", text: `Google Drive 帳號已授權:${text}` }
        : { kind: "err", text: `Google Drive 授權失敗:${text}` }
    );
    q.delete("gdrive");
    q.delete("msg");
    const rest = q.toString();
    window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
  }, [setMsg]);

  async function startOAuth(accountId?: number) {
    setBusy(true);
    try {
      const res = await api.post<{ url: string }>("/api/gdrive/oauth/start", {
        label: accountId ? "" : newLabel.trim(),
        account_id: accountId ?? null,
      });
      window.location.href = res.url;
    } catch (e: any) {
      setMsg({ kind: "err", text: `無法開始授權:${e.message}` });
      setBusy(false);
    }
  }

  async function patchAccount(acc: GDriveAccount, body: Partial<Pick<GDriveAccount, "label" | "enabled">>) {
    try {
      await api.patch(`/api/gdrive/accounts/${acc.id}`, body);
      await load();
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    }
  }

  async function renameAccount(acc: GDriveAccount) {
    const label = window.prompt("帳號顯示名稱", acc.label || acc.email);
    if (label === null) return;
    await patchAccount(acc, { label });
  }

  async function refreshAccount(acc: GDriveAccount) {
    try {
      await api.post(`/api/gdrive/accounts/${acc.id}/refresh`);
      await load();
    } catch (e: any) {
      setMsg({ kind: "err", text: `更新配額失敗:${e.message}` });
      await load();
    }
  }

  async function deleteAccount(acc: GDriveAccount) {
    const ok = await confirmDialog(
      `刪除 Drive 帳號「${acc.label || acc.email}」?`,
      "只會刪除本站的授權紀錄,不會動到 Drive 上的檔案。"
    );
    if (!ok) return;
    try {
      await api.del(`/api/gdrive/accounts/${acc.id}`);
      await load();
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    }
  }

  async function savePolicy(patch: Partial<Pick<GDrivePolicy, "daily_budget_gb" | "window" | "paused">>) {
    try {
      await api.put("/api/gdrive/policy", patch);
      setMsg({ kind: "ok", text: "Drive 流量設定已儲存" });
      await load();
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    }
  }

  if (!status) {
    return (
      <section className="rounded-lg border border-border bg-card p-4">
        <h2 className="text-lg font-semibold">Google Drive</h2>
        <p className="text-sm text-muted-foreground">讀取中…</p>
      </section>
    );
  }

  const ready = status.oauth_configured && status.key_configured;
  const policy = status.queue.policy;

  return (
    <section className="space-y-4 rounded-lg border border-border bg-card p-4">
      <div>
        <h2 className="text-lg font-semibold">Google Drive</h2>
        <p className="text-xs text-muted-foreground">
          在製作商頁勾選製作商即可把 PikPak 上的影片搬到這裡的帳號,資料夾結構與 PikPak 相同。
        </p>
      </div>

      {!ready && (
        <div className="space-y-1 rounded-md border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-200">
          <div className="inline-flex items-center gap-1 font-medium">
            <TriangleAlert className="h-3.5 w-3.5" aria-hidden /> 尚未完成設定
          </div>
          {!status.oauth_configured && <div>・.env 缺少 GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET</div>}
          {!status.key_configured && <div>・.env 缺少(或格式錯誤)GDRIVE_TOKEN_KEY</div>}
          <div>
            ・Google Cloud Console 的「已授權的重新導向 URI」要填:
            <span className="ml-1 select-all font-mono">{status.redirect_uri}</span>
          </div>
          <div>・OAuth 同意畫面需「發布」為正式版,否則授權 7 天後失效。</div>
        </div>
      )}

      <div className="space-y-2">
        <h3 className="text-sm font-medium">帳號</h3>
        {status.accounts.length === 0 && (
          <p className="text-sm text-muted-foreground">還沒有帳號。</p>
        )}
        {status.accounts.map((a) => {
          const pct = a.quota_total ? Math.min(100, (a.quota_used / a.quota_total) * 100) : 0;
          return (
            <div key={a.id} className="space-y-2 rounded-md border border-border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{a.label || a.email}</span>
                {a.label && a.email && a.label !== a.email && (
                  <span className="text-xs text-muted-foreground">{a.email}</span>
                )}
                {a.authorized && !a.last_error ? (
                  <span className="inline-flex items-center gap-1 text-xs text-emerald-300">
                    <Check className="h-3 w-3" aria-hidden /> 已授權
                  </span>
                ) : (
                  <span className="text-xs text-amber-300">需要重新授權</span>
                )}
                <label className="ml-auto inline-flex items-center gap-2 text-xs text-muted-foreground">
                  啟用
                  <Switch checked={a.enabled} onCheckedChange={(v) => patchAccount(a, { enabled: v })} />
                </label>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded bg-muted">
                <div className="h-full bg-primary" style={{ width: `${pct}%` }} />
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>
                  空間 {fmtBytes(a.quota_used, "0")} / {a.quota_total ? fmtBytes(a.quota_total) : "無上限"}
                </span>
                <span>今日已上傳 {fmtBytes(a.uploaded_today, "0")}(剩 {fmtBytes(a.remaining_today, "0")})</span>
                <span>
                  本站搬入 {a.stored_files} 檔 / {fmtBytes(a.stored_bytes, "0")}
                </span>
                {a.quota_checked_at && <span>配額更新於 {fmtDateTime(a.quota_checked_at)}</span>}
              </div>
              {a.last_error && (
                <div className="text-xs text-amber-300/90">錯誤:{a.last_error}</div>
              )}
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={() => refreshAccount(a)}>
                  <RefreshCw className="mr-1 h-3.5 w-3.5" aria-hidden /> 更新配額
                </Button>
                <Button size="sm" variant="outline" onClick={() => renameAccount(a)}>
                  改名稱
                </Button>
                <Button size="sm" variant="outline" disabled={!ready || busy} onClick={() => startOAuth(a.id)}>
                  重新授權
                </Button>
                <Button size="sm" variant="outline" onClick={() => deleteAccount(a)}>
                  刪除
                </Button>
              </div>
            </div>
          );
        })}
        <div className="flex flex-wrap gap-2">
          <Input
            className="max-w-xs"
            placeholder="新帳號顯示名稱(可留空,預設 email)"
            value={newLabel}
            onChange={(e) => setNewLabel(e.target.value)}
          />
          <Button disabled={!ready || busy} onClick={() => startOAuth()}>
            {busy ? "前往 Google…" : "新增 Google 帳號"}
          </Button>
        </div>
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-medium">流量控管(VPS)</h3>
        <p className="text-xs text-muted-foreground">
          檔案會先從 PikPak 下載到後端再上傳到 Drive,主機流量約為搬移量的 2 倍。
          超過預算或不在時段內會自動暫停,之後從中斷的位置續傳。時區:{policy.timezone}
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <label className="space-y-1 text-xs text-muted-foreground">
            <div>每日搬移上限(GB,0 = 不限)</div>
            <Input className="w-36" type="number" min={0} step={10} value={budget} onChange={(e) => setBudget(e.target.value)} />
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">
            <div>允許時段(HH:MM-HH:MM,空 = 全天)</div>
            <Input className="w-40" placeholder="01:00-07:00" value={windowStr} onChange={(e) => setWindowStr(e.target.value)} />
          </label>
          <Button
            variant="outline"
            onClick={() => savePolicy({ daily_budget_gb: Number(budget) || 0, window: windowStr.trim() })}
          >
            儲存
          </Button>
          <label className="ml-auto inline-flex items-center gap-2 text-sm">
            暫停搬移
            <Switch checked={policy.paused} onCheckedChange={(v) => savePolicy({ paused: v })} />
          </label>
        </div>
        <div className="text-xs text-muted-foreground">
          今日已搬 {fmtBytes(policy.used_today_bytes, "0")}
          {policy.remaining_today_bytes !== null && `・剩 ${fmtBytes(policy.remaining_today_bytes, "0")}`}
          {policy.blocked_reason && <span className="ml-2 text-amber-300">目前暫停:{policy.blocked_reason}</span>}
        </div>
      </div>
    </section>
  );
}
