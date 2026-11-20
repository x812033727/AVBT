"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  type GDriveAccount,
  type GDriveMigrateResult,
  type GDriveStatus,
  type StudioIndexItem,
} from "@/lib/api";
import { fmtBytes } from "@/lib/format";

// 把勾選的製作商搬到某個 Google Drive 帳號。一家製作商 = 一個搬移工作。
export default function GDriveMigrateDialog({
  open,
  studios,
  onOpenChange,
  onDone,
}: {
  open: boolean;
  studios: StudioIndexItem[];
  onOpenChange: (v: boolean) => void;
  onDone: () => void;
}) {
  const [status, setStatus] = useState<GDriveStatus | null>(null);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [deleteSource, setDeleteSource] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    api
      .get<GDriveStatus>("/api/gdrive/status")
      .then((s) => {
        setStatus(s);
        const usable = s.accounts.filter((a) => a.enabled && a.authorized);
        setAccountId((prev) => prev ?? usable[0]?.id ?? null);
      })
      .catch((e: any) => setError(e.message));
  }, [open]);

  const usable: GDriveAccount[] = (status?.accounts ?? []).filter((a) => a.enabled && a.authorized);
  const ready = !!status && status.oauth_configured && status.key_configured;
  const works = studios.reduce((n, s) => n + (s.pikpak_count ?? s.work_count), 0);

  async function submit() {
    if (!accountId) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<GDriveMigrateResult>("/api/gdrive/migrate", {
        studio_ids: studios.map((s) => s.id),
        account_id: accountId,
        delete_source: deleteSource,
      });
      const ok = res.results.filter((r) => r.job_id);
      const bad = res.results.filter((r) => !r.job_id);
      if (ok.length) toast.success(`已建立 ${ok.length} 個搬移工作,可在 Drive 頁查看進度`);
      for (const r of bad) {
        const name = studios.find((s) => s.id === r.studio_id)?.name ?? r.studio_id;
        toast.error(`${name}:${r.error}`);
      }
      onDone();
      onOpenChange(false);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>搬到 Google Drive</DialogTitle>
          <DialogDescription>
            {studios.length} 家製作商・約 {works} 部作品。Drive 上會用與 PikPak 相同的資料夾結構。
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-32 overflow-y-auto rounded border border-border p-2 text-xs text-muted-foreground">
          {studios.map((s) => (
            <div key={s.id} className="flex justify-between gap-2">
              <span className="truncate">{s.name}</span>
              <span className="shrink-0">
                {s.pikpak_count ?? s.work_count} 部
                {s.gdrive_count > 0 && `(已在 Drive ${s.gdrive_count})`}
              </span>
            </div>
          ))}
        </div>

        {!status && !error && <p className="text-sm text-muted-foreground">讀取帳號…</p>}
        {status && !ready && (
          <p className="text-sm text-amber-300">
            Google Drive 尚未設定,請先到 <Link href="/settings" className="underline">設定頁</Link> 完成。
          </p>
        )}
        {status && ready && usable.length === 0 && (
          <p className="text-sm text-amber-300">
            沒有可用的 Drive 帳號,請先到 <Link href="/settings" className="underline">設定頁</Link> 新增。
          </p>
        )}

        {usable.length > 0 && (
          <div className="space-y-2">
            <div className="text-sm font-medium">目標帳號</div>
            {usable.map((a) => {
              const free = a.quota_total ? a.quota_total - a.quota_used : null;
              return (
                <label
                  key={a.id}
                  className={
                    "flex cursor-pointer items-center gap-3 rounded-md border p-2 text-sm " +
                    (accountId === a.id ? "border-primary bg-primary/10" : "border-border")
                  }
                >
                  <input
                    type="radio"
                    name="gdrive-account"
                    checked={accountId === a.id}
                    onChange={() => setAccountId(a.id)}
                  />
                  <span className="flex-1 truncate">
                    {a.label || a.email}
                    {a.last_error && (
                      <span className="ml-2 text-xs text-amber-300" title={a.last_error}>
                        上次出錯,可能需要重新授權
                      </span>
                    )}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    剩餘 {free === null ? "無上限" : fmtBytes(Math.max(0, free), "0")}
                  </span>
                </label>
              );
            })}
          </div>
        )}

        <label className="flex items-start gap-2 text-sm">
          <Checkbox
            checked={deleteSource}
            onCheckedChange={(v) => setDeleteSource(v === true)}
            className="mt-0.5"
          />
          <span>
            全部搬完且大小核對一致後,把 PikPak 原檔丟到垃圾桶
            <span className="block text-xs text-muted-foreground">
              任何一個檔案失敗,該製作商的原檔就會全部保留。
            </span>
          </span>
        </label>

        {status?.queue.policy.blocked_reason && (
          <p className="text-xs text-amber-300">
            目前搬移暫停中({status.queue.policy.blocked_reason}),工作會先排隊。
          </p>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button disabled={!ready || !accountId || busy || studios.length === 0} onClick={submit}>
            {busy ? "建立中…" : "開始搬移"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
