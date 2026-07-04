"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import MagnetTable from "@/components/MagnetTable";
import { Skeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
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
    return (
      <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
        {error}
      </div>
    );
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
              referrerPolicy="no-referrer"
              className="w-full rounded-lg border border-white/10"
            />
          )}
        </div>
        <div className="space-y-3">
          <div>
            <div className="text-sm font-mono text-accent">{data.code}</div>
            <h1 className="text-xl font-semibold">{data.title}</h1>
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
                <dt className="text-white/40">雲端影片</dt>
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
                    className="tag hover:bg-accent/30 hover:text-white"
                  >
                    {a.name}
                  </Link>
                ) : (
                  <span key={a.name} className="tag">
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
                    className="tag hover:bg-accent/30 hover:text-white"
                  >
                    {g.name}
                  </Link>
                ) : (
                  <span key={g.name} className="tag">
                    {g.name}
                  </span>
                )
              )}
            </div>
          )}
          <div className="flex gap-2 pt-1">
            <button onClick={() => addToCollection("wishlist")} className="btn-ghost">
              加入待看
            </button>
            <button onClick={() => addToCollection("done")} className="btn-ghost">
              標記完成
            </button>
          </div>
          {savingMsg && (
            <div className="text-sm text-white/60">{savingMsg}</div>
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
            {data.samples.map((s) => (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={s}
                src={imgProxy(s)}
                alt=""
                referrerPolicy="no-referrer"
                className="rounded border border-white/10"
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
      <dt className="text-white/40">{k}</dt>
      <dd className="text-white/80">{v}</dd>
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
      <dt className="text-white/40">{k}</dt>
      <dd className="text-white/80">
        {ref.id ? (
          <Link
            href={`/${kind}/${encodeURIComponent(ref.id)}`}
            className="hover:text-accent hover:underline"
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
      <span className="text-white/40">{label} </span>
      {result.video_count > 1 ? (
        <span className="text-amber-300">{result.video_count} 部(分集)</span>
      ) : result.video_count === 1 ? (
        <span className="text-white/80">1 部(單一影片)</span>
      ) : (
        <span className="text-white/50">下載中</span>
      )}
    </span>
  );
}
