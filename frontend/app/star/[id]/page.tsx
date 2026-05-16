"use client";

import { useCallback, useEffect, useState } from "react";
import BulkSendButton from "@/components/BulkSendButton";
import MovieCard from "@/components/MovieCard";
import { api, type SearchResult, type StarProfile } from "@/lib/api";

export default function StarPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);
  const [uncensored, setUncensored] = useState(false);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<SearchResult | null>(null);
  const [profile, setProfile] = useState<StarProfile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<StarProfile | null>(
        `/api/javbus/star/${encodeURIComponent(id)}/profile?uncensored=${uncensored}`
      )
      .then((p) => {
        if (alive) setProfile(p);
      })
      .catch(() => {
        if (alive) setProfile(null);
      });
    return () => {
      alive = false;
    };
  }, [id, uncensored]);

  const run = useCallback(
    async (p: number) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({
          page: String(p),
          uncensored: String(uncensored),
        });
        const res = await api.get<SearchResult>(
          `/api/javbus/star/${encodeURIComponent(id)}?${params.toString()}`
        );
        setData(res);
        setPage(p);
      } catch (e: any) {
        setError(e.message);
        setData(null);
      } finally {
        setLoading(false);
      }
    },
    [id, uncensored]
  );

  useEffect(() => {
    run(1);
  }, [run]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-4 rounded-lg border border-white/10 bg-panel p-4">
        {profile?.avatar ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={profile.avatar}
            alt={profile.name || id}
            referrerPolicy="no-referrer"
            className="h-32 w-24 flex-none rounded object-cover"
          />
        ) : (
          <div className="grid h-32 w-24 flex-none place-items-center rounded bg-white/5 text-3xl text-white/30">
            ?
          </div>
        )}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="text-xs text-white/40">女優</div>
          <h1 className="text-xl font-semibold text-accent">
            {profile?.name || id}
          </h1>
          {profile && (
            <dl className="grid grid-cols-[64px_1fr] gap-x-2 gap-y-0.5 text-xs">
              {profile.birthday && (
                <>
                  <dt className="text-white/40">生日</dt>
                  <dd>
                    {profile.birthday}
                    {profile.age ? ` (${profile.age})` : ""}
                  </dd>
                </>
              )}
              {profile.height && (
                <>
                  <dt className="text-white/40">身高</dt>
                  <dd>{profile.height}</dd>
                </>
              )}
              {(profile.bust || profile.cup) && (
                <>
                  <dt className="text-white/40">三圍</dt>
                  <dd>
                    {[
                      profile.bust && `${profile.bust}${profile.cup ? ` (${profile.cup})` : ""}`,
                      profile.waist,
                      profile.hip,
                    ]
                      .filter(Boolean)
                      .join(" / ")}
                  </dd>
                </>
              )}
              {profile.birthplace && (
                <>
                  <dt className="text-white/40">出生地</dt>
                  <dd>{profile.birthplace}</dd>
                </>
              )}
              {profile.hobby && (
                <>
                  <dt className="text-white/40">愛好</dt>
                  <dd className="line-clamp-2">{profile.hobby}</dd>
                </>
              )}
            </dl>
          )}
        </div>
        <div className="flex flex-col items-end gap-2">
          <label className="flex items-center gap-2 text-sm text-white/70">
            <input
              type="checkbox"
              checked={uncensored}
              onChange={(e) => setUncensored(e.target.checked)}
            />
            無碼
          </label>
          <BulkSendButton
            streamPath={`/api/javbus/star/${encodeURIComponent(id)}/send-all/stream`}
            title={`送女優「${profile?.name || id}」全部`}
            defaultOptions={{ uncensored }}
          />
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {loading && <div className="text-sm text-white/50">載入中…</div>}

      {data && (
        <>
          <div className="text-sm text-white/50">
            第 {data.page} 頁
            {data.total_pages ? ` / 共 ${data.total_pages} 頁` : ""}，共{" "}
            {data.items.length} 筆
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {data.items.map((it) => (
              <MovieCard key={it.code + it.detail_url} item={it} />
            ))}
          </div>
          <div className="flex justify-center gap-2 pt-2">
            <button
              className="btn-ghost"
              disabled={loading || page <= 1}
              onClick={() => run(page - 1)}
            >
              上一頁
            </button>
            <button
              className="btn-ghost"
              disabled={loading || !data.has_next}
              onClick={() => run(page + 1)}
            >
              下一頁
            </button>
          </div>
        </>
      )}
    </div>
  );
}
