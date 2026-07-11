"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { api, downloadAuthed } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import type { SetMsg } from "./types";

type AutoBackupStatus = {
  enabled: boolean;
  interval_hours: number;
  keep: number;
  last_result: string;
  last_at: string | null;
  files: string[];
};

export default function BackupSection({ setMsg }: { setMsg: SetMsg }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [overwrite, setOverwrite] = useState(false);
  const [auto, setAuto] = useState<AutoBackupStatus | null>(null);

  const loadAuto = useCallback(async () => {
    const s = await api
      .get<AutoBackupStatus>("/api/backup/auto/status")
      .catch(() => null);
    setAuto(s);
  }, []);

  useEffect(() => {
    loadAuto();
  }, [loadAuto]);

  async function runBackupNow() {
    setBusy(true);
    try {
      const r = await api.post<{ file: string; status: AutoBackupStatus }>(
        "/api/backup/auto/run"
      );
      setAuto(r.status);
      setMsg({ kind: "ok", text: `已備份資料庫:${r.file}` });
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  async function download() {
    // Backup is a protected endpoint, so a raw <a href> link (no auth
    // header) would 401. Fetch it with the token and save the blob.
    try {
      await downloadAuthed("/api/backup", "avbt-backup.json");
    } catch (e: any) {
      setMsg({ kind: "err", text: `下載失敗：${e.message}` });
    }
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
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h2 className="text-lg font-semibold">備份 / 還原</h2>
      <p className="text-xs text-muted-foreground/80">
        匯出包含：收藏清單、追蹤的女優、所有送出紀錄。不含 PikPak token 與設定。
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" onClick={download} disabled={busy}>
          下載備份 (JSON)
        </Button>
        <Button
          variant="outline"
          onClick={() => fileRef.current?.click()}
          disabled={busy}
        >
          選擇備份檔還原…
        </Button>
        <div className="flex items-center gap-1.5">
          <Checkbox
            id="backup-overwrite"
            checked={overwrite}
            onCheckedChange={(v) => setOverwrite(v === true)}
          />
          <Label
            htmlFor="backup-overwrite"
            className="text-xs font-normal text-muted-foreground"
          >
            覆蓋現有
          </Label>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={upload}
        />
      </div>

      <div className="border-t border-border pt-3">
        <div className="text-sm font-medium text-foreground/80">自動資料庫備份</div>
        {auto ? (
          <>
            <div className="mt-1 text-xs text-muted-foreground">
              {auto.enabled
                ? `每 ${auto.interval_hours} 小時備份到 data/backups/,保留最新 ${auto.keep} 份`
                : "已停用(.env AUTO_BACKUP_ENABLED=false)"}
            </div>
            <div className="text-xs text-muted-foreground">
              上次:
              {auto.last_at ? (
                <>
                  {fmtDateTime(auto.last_at, "從未執行")}
                  {auto.last_result.startsWith("error:") ? (
                    <span className="inline-flex items-center gap-1 text-amber-300/80">
                      {" "}
                      <TriangleAlert className="h-3 w-3 shrink-0" aria-hidden />
                      {auto.last_result.slice(6)}
                    </span>
                  ) : (
                    <span className="font-mono"> {auto.last_result.replace(/^ok:/, "")}</span>
                  )}
                </>
              ) : (
                "從未執行"
              )}
              {" ・ "}現有 {auto.files.length} 份
            </div>
            <Button
              variant="outline"
              className="mt-2"
              onClick={runBackupNow}
              disabled={busy}
            >
              {busy ? "備份中…" : "立即備份"}
            </Button>
          </>
        ) : (
          <div className="mt-1 text-xs text-muted-foreground/70">載入中…</div>
        )}
      </div>
    </section>
  );
}
