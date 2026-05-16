"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import MagnetTable from "@/components/MagnetTable";
import { api, type MovieDetail } from "@/lib/api";

export default function MoviePage({ params }: { params: { code: string } }) {
  const code = decodeURIComponent(params.code);
  const [data, setData] = useState<MovieDetail | null>(null);
  const [sentHashes, setSentHashes] = useState<Set<string>>(new Set());
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
      setSavingMsg(status === "wishlist" ? "已加入待看清單" : "已標記為完成");
    } catch (e: any) {
      setSavingMsg(`儲存失敗：${e.message}`);
    }
  }

  if (error) {
    return (
      <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
        {error}
      </div>
    );
  }
  if (!data) return <div className="text-white/50">載入中…</div>;

  return (
    <div className="space-y-6">
      <div className="grid gap-6 md:grid-cols-[260px_1fr]">
        <div>
          {data.cover && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={data.cover}
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
            <Info k="導演" v={data.director} />
            <Info k="製作商" v={data.studio} />
            <Info k="發行商" v={data.label} />
            <Info k="系列" v={data.series} />
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
                src={s}
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
