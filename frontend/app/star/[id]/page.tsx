"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Clapperboard, Star, User } from "lucide-react";
import BulkSendButton from "@/components/BulkSendButton";
import MovieCard from "@/components/MovieCard";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { MovieGrid } from "@/components/shared/MovieGrid";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  api,
  imgProxy,
  type SearchResult,
  type StarProfile,
  type TrackedListing,
} from "@/lib/api";

export default function StarPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);
  const [uncensored, setUncensored] = useState(false);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<SearchResult | null>(null);
  const [profile, setProfile] = useState<StarProfile | null>(null);
  const [tracked, setTracked] = useState<TrackedListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<StarProfile | null>(
        `/api/javbus/star/${encodeURIComponent(id)}/profile?uncensored=${uncensored}`
      )
      .then((p) => alive && setProfile(p))
      .catch(() => alive && setProfile(null));
    api
      .get<TrackedListing>(`/api/tracked/star/${encodeURIComponent(id)}`)
      .then((t) => alive && setTracked(t))
      .catch(() => alive && setTracked(null));
    return () => {
      alive = false;
    };
  }, [id, uncensored]);

  async function toggleTrack() {
    if (tracked) {
      await api.del(`/api/tracked/star/${encodeURIComponent(id)}`);
      setTracked(null);
    } else {
      const t = await api.post<TrackedListing>("/api/tracked", {
        kind: "star",
        id,
        name: profile?.name || id,
        avatar: profile?.avatar || "",
        uncensored,
        auto_send: false,
      });
      setTracked(t);
    }
  }

  async function toggleAutoSend() {
    if (!tracked) return;
    const t = await api.post<TrackedListing>("/api/tracked", {
      ...tracked,
      auto_send: !tracked.auto_send,
    });
    setTracked(t);
  }

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
        if (res.items.length === 0 && p > 1) {
          setError(`已是最後一頁（第 ${p} 頁無內容）`);
          return;
        }
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
      <div className="flex flex-wrap items-start gap-4 rounded-lg border border-border bg-card p-4">
        {profile?.avatar ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={imgProxy(profile.avatar)}
            alt={profile.name || id}
            referrerPolicy="no-referrer"
            className="h-32 w-24 flex-none rounded-md object-cover"
          />
        ) : (
          <div className="grid h-32 w-24 flex-none place-items-center rounded-md bg-muted">
            <User className="h-8 w-8 text-muted-foreground/50" aria-hidden />
          </div>
        )}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="text-xs text-muted-foreground">女優</div>
          <h1 className="text-xl font-semibold text-primary">
            {profile?.name || id}
          </h1>
          {profile && (
            <dl className="grid grid-cols-[64px_1fr] gap-x-2 gap-y-0.5 text-xs">
              {profile.birthday && (
                <>
                  <dt className="text-muted-foreground">生日</dt>
                  <dd>
                    {profile.birthday}
                    {profile.age ? ` (${profile.age})` : ""}
                  </dd>
                </>
              )}
              {profile.height && (
                <>
                  <dt className="text-muted-foreground">身高</dt>
                  <dd>{profile.height}</dd>
                </>
              )}
              {(profile.bust || profile.cup) && (
                <>
                  <dt className="text-muted-foreground">三圍</dt>
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
                  <dt className="text-muted-foreground">出生地</dt>
                  <dd>{profile.birthplace}</dd>
                </>
              )}
              {profile.hobby && (
                <>
                  <dt className="text-muted-foreground">愛好</dt>
                  <dd className="line-clamp-2">{profile.hobby}</dd>
                </>
              )}
            </dl>
          )}
        </div>
        <div className="flex flex-col items-end gap-2">
          <div className="flex items-center gap-2">
            <Checkbox
              id="star-uncensored"
              checked={uncensored}
              onCheckedChange={(v) => setUncensored(v === true)}
            />
            <Label
              htmlFor="star-uncensored"
              className="text-sm font-normal text-muted-foreground"
            >
              無碼
            </Label>
          </div>
          <Button
            variant={tracked ? "outline" : "default"}
            onClick={toggleTrack}
          >
            {tracked ? (
              <>
                <Check aria-hidden />
                已追蹤
              </>
            ) : (
              <>
                <Star aria-hidden />
                追蹤
              </>
            )}
          </Button>
          {tracked && (
            <div className="flex items-center gap-1.5">
              <Checkbox
                id="star-auto-send"
                checked={tracked.auto_send}
                onCheckedChange={toggleAutoSend}
              />
              <Label
                htmlFor="star-auto-send"
                className="text-xs font-normal text-muted-foreground"
              >
                新作品自動送 PikPak
              </Label>
            </div>
          )}
          <BulkSendButton
            streamPath={`/api/javbus/star/${encodeURIComponent(id)}/send-all/stream`}
            title={`送女優「${profile?.name || id}」全部`}
            defaultOptions={{ uncensored }}
          />
        </div>
      </div>

      {error && <ErrorBox message={error} />}

      {loading && <div className="text-sm text-muted-foreground">載入中…</div>}

      {data && (
        <>
          <div className="text-sm text-muted-foreground">
            第 {data.page} 頁
            {data.total_pages ? ` / 共 ${data.total_pages} 頁` : ""}，共{" "}
            {data.items.length} 筆
          </div>
          {data.items.length === 0 ? (
            <EmptyState icon={Clapperboard} title="這一頁沒有作品" />
          ) : (
            <MovieGrid>
              {data.items.map((it) => (
                <MovieCard key={it.code + it.detail_url} item={it} />
              ))}
            </MovieGrid>
          )}
          <div className="flex items-center justify-center gap-2 pt-2">
            <Button
              variant="outline"
              disabled={loading || page <= 1}
              onClick={() => run(page - 1)}
            >
              上一頁
            </Button>
            <Button
              variant="outline"
              disabled={loading}
              onClick={() => run(page + 1)}
              title={!data.has_next ? "後端沒偵測到下一頁，但仍可嘗試" : undefined}
            >
              下一頁
            </Button>
            {!data.has_next && (
              <span className="text-xs text-muted-foreground">（已到底）</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
