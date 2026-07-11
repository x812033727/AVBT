"use client";

import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { api } from "@/lib/api";
import { fmtBytes } from "@/lib/format";
import { Skeleton } from "@/components/ui/skeleton";

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
    return <Skeleton className="h-7 rounded-md" />;
  }
  if (!stats || (stats.total_files === 0 && stats.total_folders === 0)) {
    return null;
  }

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground">
      <span>
        <span className="text-muted-foreground/60">檔案</span> {stats.total_files} ·
        <span className="ml-1 text-muted-foreground/60">資料夾</span> {stats.total_folders}
      </span>
      <span>
        <span className="text-muted-foreground/60">總大小</span>{" "}
        {fmtBytes(stats.total_size, "0 B")}
      </span>
      {stats.video_count > 0 && (
        <span>
          <span className="text-muted-foreground/60">影片</span> {stats.video_count} (
          {fmtBytes(stats.video_size, "0 B")})
        </span>
      )}
      {stats.coded_count > 0 && (
        <span>
          <span className="text-muted-foreground/60">有番號</span> {stats.coded_count}
        </span>
      )}
      {stats.archived_count > 0 && (
        <span className="inline-flex items-center gap-1 text-emerald-300/80">
          <Check className="h-3 w-3" aria-hidden />
          已歸檔 {stats.archived_count}
        </span>
      )}
      {stats.partial && (
        <span className="text-amber-300/80">(部分統計)</span>
      )}
    </div>
  );
}
