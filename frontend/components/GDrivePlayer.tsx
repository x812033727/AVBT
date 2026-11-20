"use client";

import { useEffect, useState } from "react";
import { ExternalLink, FolderOpen, HardDrive, Play } from "lucide-react";
import { api, type GDriveLocateOut, type GDriveLocationFile } from "@/lib/api";
import { fmtBytes } from "@/lib/format";

// 作品頁的 Google Drive 區塊:搬到 Drive 的作品顯示資料夾連結,
// 影片用 Drive 的 /preview 內嵌播放(檔案已設為「知道連結即可檢視」)。
// 沒有搬到 Drive 的作品不渲染任何東西。
export default function GDrivePlayer({ code }: { code: string }) {
  const [data, setData] = useState<GDriveLocateOut | null>(null);
  const [playing, setPlaying] = useState<GDriveLocationFile | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setPlaying(null);
    api
      .get<GDriveLocateOut>(`/api/gdrive/locate/${encodeURIComponent(code)}`)
      .then((res) => alive && setData(res))
      .catch(() => alive && setData(null));
    return () => {
      alive = false;
    };
  }, [code]);

  if (!data || data.locations.length === 0) return null;

  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h2 className="inline-flex items-center gap-2 text-lg font-semibold">
        <HardDrive className="h-4 w-4 text-primary" aria-hidden /> Google Drive
      </h2>

      {playing && (
        <div className="space-y-1">
          <div className="aspect-video w-full overflow-hidden rounded-md border border-border bg-black">
            <iframe
              key={playing.file_id}
              src={playing.preview_url}
              title={playing.name}
              allow="autoplay; fullscreen"
              allowFullScreen
              className="h-full w-full"
            />
          </div>
          <p className="text-xs text-muted-foreground">
            剛上傳的大檔 Drive 還在轉檔時會顯示「處理中」,稍後再試;播不了可以按「在 Drive 開啟」。
          </p>
        </div>
      )}

      {data.locations.map((loc) => (
        <div key={loc.account_id} className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted-foreground">帳號</span>
            <span className="font-medium">{loc.account_label}</span>
            {loc.folder_url && (
              <a
                href={loc.folder_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
              >
                <FolderOpen className="h-3.5 w-3.5" aria-hidden /> 開啟資料夾
              </a>
            )}
          </div>
          <div className="space-y-1 text-xs">
            {loc.files.map((f) => (
              <div key={f.file_id} className="flex items-center gap-2">
                {f.is_video && f.preview_url ? (
                  <button
                    onClick={() => setPlaying(f)}
                    className="inline-flex shrink-0 items-center gap-1 text-emerald-300 hover:underline"
                  >
                    <Play className="h-3 w-3" aria-hidden /> 播放
                  </button>
                ) : (
                  <span className="w-[42px] shrink-0" />
                )}
                <a
                  href={f.view_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex shrink-0 items-center gap-1 text-primary hover:underline"
                >
                  <ExternalLink className="h-3 w-3" aria-hidden /> 在 Drive 開啟
                </a>
                <span className="truncate font-mono text-foreground/70" title={f.path}>
                  {f.name}
                </span>
                <span className="ml-auto shrink-0 text-muted-foreground">{fmtBytes(f.size)}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}
