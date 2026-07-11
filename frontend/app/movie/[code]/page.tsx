"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import MagnetTable from "@/components/MagnetTable";
import { Skeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Button } from "@/components/ui/button";
import {
  api,
  imgProxy,
  type MovieDetail,
  type VideoCountResponse,
  type VideoCountResult,
} from "@/lib/api";

export default function MoviePage({ params }: { params: { code: string } }) {
  const code = decodeURIComponent(params.code);
  const [data, setData] = useState<MovieDetail | null>(null);
  const [sentHashes, setSentHashes] = useState<Set<string>>(new Set());
  const [cloudCount, setCloudCount] = useState<VideoCountResult | null>(null);
  const [pcloudCount, setPcloudCount] = useState<VideoCountResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savingMsg, setSavingMsg] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [detail, hashes] = await Promise.all([
          api.get<MovieDetail>(`/api/javbus/movie/${encodeURIComponent(code)}`),
          api.get<string[]>("/api/collection/sent-hashes").catch(() => []),
        ]);
        if (alive) {
          setData(detail);
          setSentHashes(new Set(hashes));
        }
      } catch (e: any) {
        if (alive) setError(e.message);
      }
    })();
    // Best-effort: how many video files does this code actually have on
    // each cloud? Independent of the detail fetch — never blocks the page.
    api
      .post<VideoCountResponse>("/api/pikpak/files/video-count", {
        items: [
          { key: "pikpak", code },
          { key: "pcloud", code, provider: "pcloud" },
        ],
      })
      .then((r) => {
        if (!alive) return;
        for (const res of r.results) {
          if (!res.ok) continue;
          if (res.key === "pikpak") setCloudCount(res);
          else if (res.key === "pcloud") setPcloudCount(res);
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [code]);

  async function addToCollection(status: string) {
    if (!data) return;
    setSavingMsg(null);
    try {
      await api.post("/api/collection", {
        code: data.code,
        title: data.title,
        cover: data.cover,
        release_date: data.release_date,
        duration: data.duration,
        actresses: data.actresses.map((a) => a.name),
        genres: data.genres.map((g) => g.name),
        status,
      });
      const msg = status === "wishlist" ? "已加入待看清單" : "已標記為完成";
      setSavingMsg(msg);
      toast.success(msg);
    } catch (e: any) {
      setSavingMsg(`儲存失敗：${e.message}`);
      toast.error(e.message);
    }
  }

  if (error) {
    return <ErrorBox message={error} />;
  }
  if (!data) {
    return (
      <div className="grid gap-6 md:grid-cols-[260px_1fr]">
        <Skeleton className="aspect-[3/4] w-full" />
        <div className="space-y-3">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-6 w-3/4" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-32 w-full" />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-6 md:grid-cols-[260px_1fr]">
        <div>
          {data.cover && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={imgProxy(data.cover)}
              alt={data.code}
              loading="lazy"
              referrerPolicy="no-referrer"
              className="w-full rounded-lg border border-border"
            />
          )}
        </div>
        <div className="space-y-3 rounded-lg border border-border bg-card p-4">
          <div>
            <div className="font-mono text-sm text-primary">{data.code}</div>
            <h1 className="text-xl font-semibold text-foreground">
              {data.title}
            </h1>
          </div>
          <dl className="grid grid-cols-[80px_1fr] gap-x-3 gap-y-1 text-sm">
            <Info k="發行日期" v={data.release_date} />
            <Info k="長度" v={data.duration} />
            <RefInfo k="導演" kind="director" ref={data.director} />
            <RefInfo k="製作商" kind="studio" ref={data.studio} />
            <RefInfo k="發行商" kind="label" ref={data.label} />
            <RefInfo k="系列" kind="series" ref={data.series} />
            {(cloudCount || pcloudCount) && (
              <>
                <dt className="text-muted-foreground">雲端影片</dt>
                <dd className="space-x-2">
                  {cloudCount && (
                    <CloudCountLabel label="PikPak" result={cloudCount} />
                  )}
                  {pcloudCount && (
                    <CloudCountLabel label="pCloud" result={pcloudCount} />
                  )}
                </dd>
              </>
            )}
          </dl>
          {!!data.actresses.length && (
            <div className="flex flex-wrap gap-1">
              {data.actresses.map((a) =>
                a.id ? (
                  <Link
                    key={a.name}
                    href={`/star/${encodeURIComponent(a.id)}`}
                    className="rounded border border-border bg-muted/50 px-2 py-0.5 text-xs text-foreground/80 transition hover:border-primary hover:text-primary"
                  >
                    {a.name}
                  </Link>
                ) : (
                  <span
                    key={a.name}
                    className="rounded border border-border bg-muted/50 px-2 py-0.5 text-xs text-foreground/80"
                  >
                    {a.name}
                  </span>
                )
              )}
            </div>
          )}
          {!!data.genres.length && (
            <div className="flex flex-wrap gap-1">
              {data.genres.map((g) =>
                g.id ? (
                  <Link
                    key={g.name}
                    href={`/genre/${encodeURIComponent(g.id)}`}
                    className="rounded border border-border bg-muted/50 px-2 py-0.5 text-xs text-foreground/80 transition hover:border-primary hover:text-primary"
                  >
                    {g.name}
                  </Link>
                ) : (
                  <span
                    key={g.name}
                    className="rounded border border-border bg-muted/50 px-2 py-0.5 text-xs text-foreground/80"
                  >
                    {g.name}
                  </span>
                )
              )}
            </div>
          )}
          <div className="flex gap-2 pt-1">
            <Button
              variant="outline"
              size="sm"
              onClick={() => addToCollection("wishlist")}
            >
              加入待看
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => addToCollection("done")}
            >
              標記完成
            </Button>
          </div>
          {savingMsg && (
            <div className="text-sm text-muted-foreground">{savingMsg}</div>
          )}
        </div>
      </div>

      <section>
        <h2 className="mb-2 text-lg font-semibold">磁力連結</h2>
        <MagnetTable
          magnets={data.magnets}
          code={data.code}
          sentHashes={sentHashes}
        />
      </section>

      {!!data.samples.length && (
        <section>
          <h2 className="mb-2 text-lg font-semibold">樣品圖</h2>
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 lg:grid-cols-5">
            {data.samples.map((s, i) => (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={s}
                src={imgProxy(s)}
                alt={`${code} 樣品圖 ${i + 1}`}
                loading="lazy"
                referrerPolicy="no-referrer"
                className="rounded border border-border"
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function Info({ k, v }: { k: string; v: string }) {
  if (!v) return null;
  return (
    <>
      <dt className="text-muted-foreground">{k}</dt>
      <dd className="text-foreground/80">{v}</dd>
    </>
  );
}

function RefInfo({
  k,
  kind,
  ref,
}: {
  k: string;
  kind: "studio" | "label" | "series" | "director";
  ref: { name: string; id: string } | null;
}) {
  if (!ref || !ref.name) return null;
  return (
    <>
      <dt className="text-muted-foreground">{k}</dt>
      <dd className="text-foreground/80">
        {ref.id ? (
          <Link
            href={`/${kind}/${encodeURIComponent(ref.id)}`}
            className="hover:text-primary hover:underline"
          >
            {ref.name}
          </Link>
        ) : (
          ref.name
        )}
      </dd>
    </>
  );
}

function CloudCountLabel({
  label,
  result,
}: {
  label: string;
  result: VideoCountResult;
}) {
  const tip =
    (result.entries.length
      ? result.entries.map((e) => `${e.path}(${e.video_count})`).join("\n")
      : result.video_names.join("\n")) +
    (result.source === "transfer" ? "\n(依轉存紀錄計算)" : "");
  return (
    <span title={tip.trim() || undefined}>
      <span className="text-muted-foreground">{label} </span>
      {result.video_count > 1 ? (
        <span className="text-amber-300">{result.video_count} 部(分集)</span>
      ) : result.video_count === 1 ? (
        <span className="text-foreground/80">1 部(單一影片)</span>
      ) : (
        <span className="text-muted-foreground/70">下載中</span>
      )}
    </span>
  );
}
