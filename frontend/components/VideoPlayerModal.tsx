"use client";

import { useEffect, useState } from "react";
import { Download, ExternalLink, Play } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Links = {
  url: string;
  download_url: string;
  play_url: string;
  mime_type: string;
};

export default function VideoPlayerModal({
  open,
  file,
  onClose,
}: {
  open: boolean;
  file: { id: string; name: string } | null;
  onClose: () => void;
}) {
  const [links, setLinks] = useState<Links | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [playFailed, setPlayFailed] = useState(false);

  useEffect(() => {
    if (!open || !file) return;
    let alive = true;
    setLinks(null);
    setError(null);
    setPlayFailed(false);
    setLoading(true);
    api
      .get<Links>(`/api/pikpak/files/${encodeURIComponent(file.id)}/url`)
      .then((res) => {
        if (alive) setLinks(res);
      })
      .catch((e: any) => {
        if (alive) {
          setError(e.message || "讀取播放連結失敗");
          toast.error(e.message || "讀取播放連結失敗");
        }
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [open, file]);

  const playUrl = links?.play_url || "";
  const downloadUrl = links?.download_url || "";

  // Dialog 內容在關閉時整段 unmount,<video> 隨之卸載 → 播放必定停止
  // (與舊版條件渲染行為一致);focus trap / Esc / 捲動鎖交給 Radix。
  return (
    <Dialog open={open && !!file} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-5xl gap-0 overflow-hidden border-border bg-black p-0">
        {file && (
          <>
            <DialogHeader className="border-b border-white/10 px-4 py-2 pr-10">
              <DialogTitle
                className="flex items-center gap-2 truncate text-sm font-normal text-white/90"
                title={file.name}
              >
                <Play className="h-4 w-4 shrink-0" aria-hidden />
                <span className="truncate">{file.name}</span>
              </DialogTitle>
            </DialogHeader>

            <div className="flex aspect-video items-center justify-center bg-black">
              {loading && (
                <div className="text-sm text-white/50">準備串流連結…</div>
              )}
              {!loading && error && (
                <div className="px-4 text-center text-sm text-red-300">
                  {error}
                </div>
              )}
              {!loading && !error && playUrl && !playFailed && (
                // eslint-disable-next-line jsx-a11y/media-has-caption
                <video
                  key={playUrl}
                  src={playUrl}
                  controls
                  autoPlay
                  playsInline
                  preload="metadata"
                  className="h-full w-full"
                  onError={() => setPlayFailed(true)}
                />
              )}
              {!loading && !error && playFailed && (
                <div className="space-y-2 px-4 text-center text-sm text-white/70">
                  <div>
                    瀏覽器無法直接播放此格式(常見於 MKV / AVI 等)
                  </div>
                  <div className="text-xs text-white/40">
                    請改用「下載」或「新分頁開啟」
                  </div>
                </div>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-3 border-t border-white/10 px-4 py-2 text-xs text-white/60">
              <span className="text-white/40">按 Esc 關閉</span>
              <div className="ml-auto flex gap-2">
                {downloadUrl && (
                  <Button
                    asChild
                    variant="ghost"
                    size="sm"
                    className="text-white/80 hover:bg-white/10 hover:text-white"
                  >
                    <a href={downloadUrl} target="_blank" rel="noreferrer">
                      <Download aria-hidden />
                      下載
                    </a>
                  </Button>
                )}
                {playUrl && playUrl !== downloadUrl && (
                  <Button
                    asChild
                    variant="ghost"
                    size="sm"
                    className="text-white/80 hover:bg-white/10 hover:text-white"
                  >
                    <a href={playUrl} target="_blank" rel="noreferrer">
                      <ExternalLink aria-hidden />
                      新分頁開啟
                    </a>
                  </Button>
                )}
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
