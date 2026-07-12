"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ChevronLeft } from "lucide-react";
import MovieCard from "@/components/MovieCard";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { MovieGrid, MovieGridSkeleton } from "@/components/shared/MovieGrid";
import { api, type StudioSeriesWorksOut } from "@/lib/api";

// 單一系列的已下載作品格線。MovieCard 連到 /movie/{code} 作品介紹+圖片+播放。
export default function StudioSeriesWorksPage({
  params,
}: {
  params: { id: string; seriesId: string };
}) {
  const studioId = decodeURIComponent(params.id);
  const seriesId = decodeURIComponent(params.seriesId);
  const [data, setData] = useState<StudioSeriesWorksOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<StudioSeriesWorksOut>(
        `/api/studios/${encodeURIComponent(
          studioId
        )}/series/${encodeURIComponent(seriesId)}/works`
      )
      .then((res) => alive && setData(res))
      .catch((e: any) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [studioId, seriesId]);

  if (error) return <ErrorBox message={error} />;
  if (!data) return <MovieGridSkeleton count={10} />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href={`/studios/${encodeURIComponent(studioId)}`}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4" aria-hidden />
          {data.studio_name}
        </Link>
        <div>
          <h1 className="text-xl font-semibold">{data.series_name}</h1>
          <div className="text-sm text-muted-foreground">
            已下載 {data.count} 部
          </div>
        </div>
      </div>

      <MovieGrid>
        {data.works.map((w) => (
          <MovieCard key={w.code} item={w} present={true} />
        ))}
      </MovieGrid>
    </div>
  );
}
