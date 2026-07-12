"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Building2, ExternalLink, Layers } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Skeleton } from "@/components/Skeleton";
import { api, imgProxy, type StudioSeriesOut } from "@/lib/api";

// 單一製作商底下的系列格線。點系列進到 /studios/{id}/{seriesId} 影片列表。
export default function StudioSeriesPage({
  params,
}: {
  params: { id: string };
}) {
  const studioId = decodeURIComponent(params.id);
  const [data, setData] = useState<StudioSeriesOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<StudioSeriesOut>(
        `/api/studios/${encodeURIComponent(studioId)}/series`
      )
      .then((res) => alive && setData(res))
      .catch((e: any) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [studioId]);

  if (error) return <ErrorBox message={error} />;
  if (!data) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
        {Array.from({ length: 12 }).map((_, i) => (
          <Skeleton key={i} className="h-40 w-full" />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="grid h-14 w-14 place-items-center rounded-md bg-muted text-muted-foreground/50">
          <Building2 className="h-6 w-6" aria-hidden />
        </div>
        <div>
          <h1 className="text-xl font-semibold">{data.studio_name}</h1>
          <div className="text-sm text-muted-foreground">
            {data.series_count} 系列・已下載 {data.work_count} 部
          </div>
        </div>
        <Link
          href={`/studio/${encodeURIComponent(data.studio_id)}`}
          className="ml-auto inline-flex items-center gap-1 text-sm text-primary hover:underline"
        >
          查看 JavBus 全部作品
          <ExternalLink className="h-3.5 w-3.5" aria-hidden />
        </Link>
      </div>

      {data.series.length === 0 ? (
        <EmptyState icon={Layers} title="這個製作商還沒有已下載的作品" />
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {data.series.map((s) => (
            <Link
              key={s.id}
              href={`/studios/${encodeURIComponent(
                data.studio_id
              )}/series/${encodeURIComponent(s.id)}`}
              className="group flex flex-col overflow-hidden rounded-lg border border-border bg-card transition hover:border-primary"
            >
              <div className="aspect-[3/2] w-full overflow-hidden bg-muted">
                {s.sample_cover ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={imgProxy(s.sample_cover)}
                    alt={s.name}
                    loading="lazy"
                    referrerPolicy="no-referrer"
                    className="h-full w-full object-cover transition group-hover:scale-105"
                  />
                ) : (
                  <div className="grid h-full w-full place-items-center text-muted-foreground/40">
                    <Layers className="h-8 w-8" aria-hidden />
                  </div>
                )}
              </div>
              <div className="flex flex-1 flex-col gap-1 p-3">
                <div className="truncate text-sm text-foreground group-hover:text-primary">
                  {s.name}
                </div>
                <div className="text-xs text-muted-foreground">
                  {s.work_count} 部
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
