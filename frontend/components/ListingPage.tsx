"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, PackageSearch, Star } from "lucide-react";
import BulkSendButton from "@/components/BulkSendButton";
import MovieCard from "@/components/MovieCard";
import { MovieGridSkeleton } from "@/components/Skeleton";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { MovieGrid } from "@/components/shared/MovieGrid";
import { PresenceSummary } from "@/components/shared/PresenceSummary";
import { toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  api,
  type MissingCodesResult,
  type SearchResult,
  type TrackedKind,
  type TrackedListing,
} from "@/lib/api";

export default function ListingPage({
  kind,
  id,
  label,
  headerSlot,
  trackName,
  trackAvatar,
  trackDisabled,
}: {
  /** JavBus URL kind */
  kind: "studio" | "label" | "series" | "director" | "genre" | "star";
  /** JavBus slug (the bit after /{kind}/) */
  id: string;
  /** Human-readable label, e.g. "製作商" */
  label: string;
  /**
   * Replaces the default title block (label + name/slug) above the
   * controls row. Function form receives the current `uncensored` state
   * so callers (e.g. the star page's profile card) can keep their own
   * fetches in sync with it.
   */
  headerSlot?: React.ReactNode | ((ctx: { uncensored: boolean }) => React.ReactNode);
  /** Display name to send with the track POST instead of "" (e.g. star profile name) */
  trackName?: string;
  /** Disable the track button until caller-side data (e.g. the star
   *  profile whose avatar the track POST captures) has settled. */
  trackDisabled?: boolean;
  /** Avatar URL to send with the track POST instead of "" (e.g. star profile avatar) */
  trackAvatar?: string;
}) {
  const [uncensored, setUncensored] = useState(false);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<SearchResult | null>(null);
  const [tracked, setTracked] = useState<TrackedListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [presence, setPresence] = useState<Set<string> | null>(null);
  const [presenceMeta, setPresenceMeta] = useState<{
    total: number;
    missing: number;
    extras: number;
    expected_root: string;
  } | null>(null);
  const [presenceBusy, setPresenceBusy] = useState(false);
  const [presenceError, setPresenceError] = useState<string | null>(null);
  const [hideDownloaded, setHideDownloaded] = useState(false);
  // 全部六種 kind 都可追蹤;類別目錄很大,tracked 頁開 auto_send 前會
  // 另行 confirm,補檔量由 backfill batch limit 節流。
  const trackable = true;

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
          `/api/javbus/${kind}/${encodeURIComponent(id)}?${params.toString()}`
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
    [kind, id, uncensored]
  );

  useEffect(() => {
    run(1);
  }, [run]);

  useEffect(() => {
    if (!trackable) return;
    let alive = true;
    api
      .get<TrackedListing>(`/api/tracked/${kind}/${encodeURIComponent(id)}`)
      .then((t) => alive && setTracked(t))
      .catch(() => alive && setTracked(null));
    return () => {
      alive = false;
    };
  }, [kind, id, trackable]);

  const loadPresence = useCallback(
    async (refresh: boolean) => {
      if (!trackable || !tracked) return;
      setPresenceBusy(true);
      setPresenceError(null);
      try {
        const params = new URLSearchParams({
          uncensored: String(uncensored),
        });
        if (refresh) params.set("refresh", "true");
        const r = await api.get<MissingCodesResult>(
          `/api/tracked/${kind}/${encodeURIComponent(id)}/missing-codes?${params}`
        );
        setPresence(new Set(r.present_codes));
        setPresenceMeta({
          total: r.total,
          missing: r.missing.length,
          extras: r.extras.length,
          expected_root: r.expected_root,
        });
      } catch (e: any) {
        // Don't swallow: a JavBus 429/5xx (or any /missing-codes failure)
        // used to make the whole presence card vanish with no explanation.
        // Surface it so the user knows the count is unavailable, not zero.
        const msg = e?.message || "讀取缺漏失敗";
        setPresence(null);
        setPresenceMeta(null);
        setPresenceError(msg);
        if (refresh) toast.error(`缺漏重算失敗：${msg}`);
      } finally {
        setPresenceBusy(false);
      }
    },
    [kind, id, uncensored, trackable, tracked]
  );

  useEffect(() => {
    loadPresence(false);
  }, [loadPresence]);

  async function toggleTrack() {
    if (tracked) {
      await api.del(`/api/tracked/${kind}/${encodeURIComponent(id)}`);
      setTracked(null);
    } else {
      // Send name="" (default) so the backend pulls the real display name
      // from the listing page header (e.g. "SODクリエイト" instead of slug
      // "ca"). Callers that already have the name/avatar on hand (e.g. the
      // star page's profile fetch) can pass trackName/trackAvatar to avoid
      // losing the avatar to that server-side resolution.
      const t = await api.post<TrackedListing>("/api/tracked", {
        kind: kind as TrackedKind,
        id,
        name: trackName ?? "",
        avatar: trackAvatar ?? "",
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

  const firstTitle = data?.items?.[0]?.title || "";

  const renderedHeaderSlot =
    typeof headerSlot === "function" ? headerSlot({ uncensored }) : headerSlot;

  // Hide-downloaded only filters the items already on this (server-paginated)
  // page — it can't reach ahead into pages that haven't been fetched yet.
  const visibleItems =
    hideDownloaded && presence && tracked
      ? (data?.items ?? []).filter((it) => !presence.has(it.code))
      : data?.items ?? [];

  return (
    <div className="space-y-4">
      {renderedHeaderSlot && <div>{renderedHeaderSlot}</div>}
      <div className="flex flex-wrap items-center gap-3">
        {!renderedHeaderSlot && (
          <div>
            <div className="text-xs text-muted-foreground">{label}</div>
            {tracked?.name ? (
              <>
                <h1 className="text-lg font-semibold text-foreground">
                  {tracked.name}
                </h1>
                <div className="font-mono text-xs text-muted-foreground">
                  slug: {id}
                </div>
              </>
            ) : (
              <h1 className="font-mono text-lg text-primary">{id}</h1>
            )}
          </div>
        )}
        <div className="flex items-center gap-2">
          <Checkbox
            id="listing-uncensored"
            checked={uncensored}
            onCheckedChange={(v) => setUncensored(v === true)}
          />
          <Label
            htmlFor="listing-uncensored"
            className="text-sm font-normal text-muted-foreground"
          >
            無碼
          </Label>
        </div>
        <div className="ml-auto flex flex-col items-end gap-2">
          <div className="flex gap-2">
            {trackable && (
              <Button
                variant={tracked ? "outline" : "default"}
                onClick={toggleTrack}
                disabled={!tracked && !!trackDisabled}
                title={!tracked && trackDisabled
                  ? "載入女優資料中…(避免追蹤時遺失頭像)" : undefined}
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
            )}
            <BulkSendButton
              streamPath={`/api/javbus/${kind}/${encodeURIComponent(id)}/send-all/stream`}
              title={`送${label}「${id}」全部`}
              defaultOptions={{ uncensored }}
            />
          </div>
          {tracked && (
            <div className="flex items-center gap-1.5">
              <Checkbox
                id="listing-auto-send"
                checked={tracked.auto_send}
                onCheckedChange={toggleAutoSend}
              />
              <Label
                htmlFor="listing-auto-send"
                className="text-xs font-normal text-muted-foreground"
              >
                新作品自動送 PikPak
              </Label>
            </div>
          )}
        </div>
      </div>

      {error && <ErrorBox message={error} />}

      {loading && !data && <MovieGridSkeleton count={10} />}

      {data && (
        <>
          <div className="text-sm text-muted-foreground">
            第 {data.page} 頁
            {data.total_pages ? ` / 共 ${data.total_pages} 頁` : ""}，共{" "}
            {data.items.length} 筆
            {firstTitle && (
              <span className="ml-2 text-muted-foreground/60">
                ・最新：{firstTitle}
              </span>
            )}
          </div>
          {tracked && (
            <PresenceSummary
              meta={presenceMeta}
              error={presenceError}
              busy={presenceBusy}
              onRefresh={() => loadPresence(true)}
              hideDownloaded={hideDownloaded}
              onHideDownloadedChange={setHideDownloaded}
            />
          )}
          {data.items.length === 0 ? (
            <EmptyState icon={PackageSearch} title="這一頁沒有作品" />
          ) : visibleItems.length === 0 ? (
            <EmptyState icon={Check} title="這一頁都已下載" />
          ) : (
            <MovieGrid>
              {visibleItems.map((it) => (
                <MovieCard
                  key={it.code + it.detail_url}
                  item={it}
                  present={presence ? presence.has(it.code) : undefined}
                />
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
