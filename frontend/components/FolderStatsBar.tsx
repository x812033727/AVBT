"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type FolderStats = {
  total_files: number;
  total_folders: number;
  total_size: number;
  video_count: number;
  video_size: number;
  coded_count: number;
  archived_count: number;
  partial: boolean;
};

function fmtBytes(n: number) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(2)} ${u[i]}`;
}

export default function FolderStatsBar({ parentId }: { parentId: string }) {
  const [stats, setStats] = useState<FolderStats | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api
      .get<FolderStats>(
        `/api/pikpak/files/stats?parent_id=${encodeURIComponent(parentId)}`
      )
      .then((res) => {
        if (alive) setStats(res);
      })
      .catch(() => {
        if (alive) setStats(null);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [parentId]);

  if (loading && !stats) {
    return (
      <div className="h-7 animate-pulse rounded-md border border-white/5 bg-white/5" />
    );
  }
  if (!stats || (stats.total_files === 0 && stats.total_folders === 0)) {
    return null;
  }

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-white/5 bg-white/[0.03] px-3 py-1.5 text-xs text-white/60">
      <span>
        <span className="text-white/40">檔案</span> {stats.total_files} ·
        <span className="ml-1 text-white/40">資料夾</span> {stats.total_folders}
      </span>
      <span>
        <span className="text-white/40">總大小</span> {fmtBytes(stats.total_size)}
      </span>
      {stats.video_count > 0 && (
        <span>
          <span className="text-white/40">影片</span> {stats.video_count} (
          {fmtBytes(stats.video_size)})
        </span>
      )}
      {stats.coded_count > 0 && (
        <span>
          <span className="text-white/40">有番號</span> {stats.coded_count}
        </span>
      )}
      {stats.archived_count > 0 && (
        <span className="text-emerald-300/80">
          ✓ 已歸檔 {stats.archived_count}
        </span>
      )}
      {stats.partial && (
        <span className="text-amber-300/80">(部分統計)</span>
      )}
    </div>
  );
}
