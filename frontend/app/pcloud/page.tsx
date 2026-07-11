"use client";

import { useCallback, useEffect, useState } from "react";
import FilesTab from "@/components/pcloud/FilesTab";
import LoginPanel from "@/components/pcloud/LoginPanel";
import TransfersTab from "@/components/pcloud/TransfersTab";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, type PCloudStatus } from "@/lib/api";

// pCloud 頁薄殼:只保留登入狀態載入與 tab 切換;
// 兩個分頁的資料流與輪詢邏輯都在 components/pcloud/*。
export default function PCloudPage() {
  const [tab, setTab] = useState<"files" | "transfers">("files");
  const [status, setStatus] = useState<PCloudStatus | null>(null);

  const reloadStatus = useCallback(async () => {
    try {
      const s = await api.get<PCloudStatus>("/api/pcloud/status");
      setStatus(s);
    } catch {
      // status endpoint may fail if backend down; surface in the panel instead
    }
  }, []);

  useEffect(() => {
    reloadStatus();
  }, [reloadStatus]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Tabs
          value={tab}
          onValueChange={(v) => setTab(v as "files" | "transfers")}
        >
          <TabsList>
            <TabsTrigger value="files">雲端檔案</TabsTrigger>
            <TabsTrigger value="transfers">PikPak 傳輸佇列</TabsTrigger>
          </TabsList>
        </Tabs>
        {status && !status.logged_in && (
          <span className="ml-2 text-xs text-amber-300/80">
            未登入 — 部分功能需先登入 pCloud
          </span>
        )}
      </div>

      {!status?.logged_in && <LoginPanel status={status} onChanged={reloadStatus} />}

      {tab === "files" && <FilesTab loggedIn={!!status?.logged_in} />}
      {tab === "transfers" && <TransfersTab loggedIn={!!status?.logged_in} />}
    </div>
  );
}
