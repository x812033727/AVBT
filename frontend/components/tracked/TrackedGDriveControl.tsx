"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { HardDrive, TriangleAlert } from "lucide-react";
import { confirmDialog, toast } from "@/components/Toast";
import { Checkbox } from "@/components/ui/checkbox";
import { api, type GDriveAccount, type TrackedListing } from "@/lib/api";

// 追蹤頁每一列的「存到 Google Drive」設定。打開 = 現有作品馬上排入搬移,
// 之後歸檔完成的新作品也會自動搬到同一個帳號(後端 gdrive_auto)。

// 所有列共用一次帳號清單請求。
let accountsPromise: Promise<GDriveAccount[]> | null = null;
function loadAccounts(): Promise<GDriveAccount[]> {
  if (!accountsPromise) {
    accountsPromise = api.get<GDriveAccount[]>("/api/gdrive/accounts").catch(() => {
      accountsPromise = null;
      return [];
    });
  }
  return accountsPromise;
}

export default function TrackedGDriveControl({
  item,
  onChanged,
}: {
  item: TrackedListing;
  onChanged: () => void;
}) {
  const [accounts, setAccounts] = useState<GDriveAccount[] | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    loadAccounts().then((a) => alive && setAccounts(a));
    return () => {
      alive = false;
    };
  }, []);

  if (item.kind === "genre") return null;

  const usable = (accounts ?? []).filter((a) => a.enabled && a.authorized);
  const current = usable.find((a) => a.id === item.gdrive_account_id);
  const accountId = current?.id ?? usable[0]?.id ?? 0;
  const path = `/api/tracked/${item.kind}/${encodeURIComponent(item.id)}/gdrive`;

  async function save(next: { enabled?: boolean; account_id?: number; delete_source?: boolean }) {
    const body = {
      enabled: next.enabled ?? item.gdrive_enabled,
      account_id: next.account_id ?? accountId,
      delete_source: next.delete_source ?? item.gdrive_delete_source,
    };
    if (body.enabled && !item.gdrive_enabled) {
      const acc = usable.find((a) => a.id === body.account_id);
      const ok = await confirmDialog(
        `把「${item.name || item.id}」存到 Google Drive(${acc?.label || acc?.email || "?"})?`,
        "現有作品會立刻開始搬移,之後下載完成的新作品也會自動搬過去" +
          (body.delete_source ? ";全部核對一致後才把 PikPak 原檔丟垃圾桶。" : ";PikPak 原檔保留。")
      );
      if (!ok) return;
    }
    setBusy(true);
    try {
      await api.put(path, body);
      if (body.enabled && !item.gdrive_enabled) toast.success("已開始安排搬移,進度在 Drive 頁");
      onChanged();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (accounts !== null && usable.length === 0 && !item.gdrive_enabled) {
    return (
      <Link href="/settings" className="text-muted-foreground/70 hover:underline" title="先到設定頁新增 Google 帳號">
        <HardDrive className="mr-1 inline h-3 w-3 align-[-1px]" aria-hidden />
        存到 Drive(未設定帳號)
      </Link>
    );
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <label
          className="flex items-center gap-1.5 text-muted-foreground"
          title="開啟後:現有作品搬到選的 Drive 帳號,之後新下載的作品歸檔完成也會自動搬"
        >
          <Checkbox
            checked={item.gdrive_enabled}
            disabled={busy || accounts === null}
            onCheckedChange={(v) => save({ enabled: v === true })}
          />
          存到 Drive
        </label>
        <select
          className="h-6 max-w-[9rem] rounded border border-border bg-background px-1 text-xs"
          value={accountId || ""}
          disabled={busy || usable.length === 0}
          onChange={(e) => save({ account_id: Number(e.target.value) })}
          aria-label="Drive 帳號"
        >
          {usable.map((a) => (
            <option key={a.id} value={a.id}>
              {a.label || a.email}
            </option>
          ))}
        </select>
      </div>
      {item.gdrive_enabled && (
        <label className="flex items-center gap-1.5 text-muted-foreground" title="全部檔案核對一致後才丟 PikPak 垃圾桶">
          <Checkbox
            checked={item.gdrive_delete_source}
            disabled={busy}
            onCheckedChange={(v) => save({ delete_source: v === true })}
          />
          搬完刪 PikPak
        </label>
      )}
      {(item.gdrive_moved_count > 0 || item.gdrive_active) && (
        <div className="text-sky-300/90">
          {item.gdrive_moved_count > 0 && `Drive 已搬 ${item.gdrive_moved_count} 部`}
          {item.gdrive_active && (
            <Link href="/gdrive" className="ml-1 text-amber-300 hover:underline">
              搬移中
            </Link>
          )}
        </div>
      )}
      {item.gdrive_enabled && item.gdrive_account_id > 0 && !current && accounts !== null && (
        <div className="text-amber-300/90">原帳號已停用或刪除,請改選帳號</div>
      )}
      {item.gdrive_last_error && (
        <div className="max-w-[18rem] text-right text-amber-300/90">
          <TriangleAlert className="mr-1 inline h-3 w-3 align-[-1px]" aria-hidden />
          {item.gdrive_last_error}
        </div>
      )}
    </div>
  );
}
