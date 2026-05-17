"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "@/components/Toast";

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

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open || !file) return null;

  const playUrl = links?.play_url || "";
  const downloadUrl = links?.download_url || "";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-5xl flex-col overflow-hidden rounded-lg border border-white/10 bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-2 border-b border-white/10 px-4 py-2">
          <div className="truncate text-sm text-white/90" title={file.name}>
            ▶ {file.name}
          </div>
          <button
            onClick={onClose}
            className="text-white/60 hover:text-white"
            aria-label="關閉"
          >
            ✕
          </button>
        </div>

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
              <a
                href={downloadUrl}
                target="_blank"
                rel="noreferrer"
                className="btn-ghost"
              >
                ⤓ 下載
              </a>
            )}
            {playUrl && playUrl !== downloadUrl && (
              <a
                href={playUrl}
                target="_blank"
                rel="noreferrer"
                className="btn-ghost"
              >
                ↗ 新分頁開啟
              </a>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
