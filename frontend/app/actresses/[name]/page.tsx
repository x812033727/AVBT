"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ExternalLink, Users } from "lucide-react";
import MovieCard from "@/components/MovieCard";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { MovieGrid, MovieGridSkeleton } from "@/components/shared/MovieGrid";
import { api, imgProxy, type ActressWorksOut } from "@/lib/api";

// 單一女優的已下載作品格線。MovieCard 本身連到 /movie/{code} 作品介紹。
export default function ActressWorksPage({
  params,
}: {
  params: { name: string };
}) {
  const name = decodeURIComponent(params.name);
  const [data, setData] = useState<ActressWorksOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<ActressWorksOut>(
        `/api/actresses/${encodeURIComponent(name)}/works`
      )
      .then((res) => alive && setData(res))
      .catch((e: any) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [name]);

  if (error) return <ErrorBox message={error} />;
  if (!data) return <MovieGridSkeleton count={10} />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        {data.avatar ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={imgProxy(data.avatar)}
            alt={data.name}
            referrerPolicy="no-referrer"
            className="h-14 w-14 rounded-full border border-border object-cover"
          />
        ) : (
          <div className="grid h-14 w-14 place-items-center rounded-full bg-muted text-muted-foreground/50">
            <Users className="h-6 w-6" aria-hidden />
          </div>
        )}
        <div>
          <h1 className="text-xl font-semibold">{data.name}</h1>
          <div className="text-sm text-muted-foreground">
            已下載 {data.count} 部
          </div>
        </div>
        {data.id && (
          <Link
            href={`/star/${encodeURIComponent(data.id)}`}
            className="ml-auto inline-flex items-center gap-1 text-sm text-primary hover:underline"
          >
            查看 JavBus 全部作品
            <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          </Link>
        )}
      </div>

      <MovieGrid>
        {data.works.map((w) => (
          <MovieCard key={w.code} item={w} present={true} />
        ))}
      </MovieGrid>
    </div>
  );
}
