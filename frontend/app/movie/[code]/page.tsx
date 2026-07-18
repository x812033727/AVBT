"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  type LucideIcon,
  Building2,
  ChevronDown,
  ChevronRight,
  CalendarDays,
  Clapperboard,
  Clock,
  Play,
  Tag,
  User,
} from "lucide-react";
import FinalizeButton from "@/components/FinalizeButton";
import MagnetTable from "@/components/MagnetTable";
import { Skeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
import VideoPlayerModal from "@/components/VideoPlayerModal";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Button } from "@/components/ui/button";
import {
  api,
  imgProxy,
  type MovieDetail,
  type PartEstimate,
  type PresenceCodeFiles,
  type VideoCountResponse,
  type VideoCountResult,
} from "@/lib/api";

export default function MoviePage({ params }: { params: { code: string } }) {
  const code = decodeURIComponent(params.code);
  const [data, setData] = useState<MovieDetail | null>(null);
  const [magnetsOpen, setMagnetsOpen] = useState(false);
  const [sentHashes, setSentHashes] = useState<Set<string>>(new Set());
  const [cloudCount, setCloudCount] = useState<VideoCountResult | null>(null);
  const [pcloudCount, setPcloudCount] = useState<VideoCountResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savingMsg, setSavingMsg] = useState<string | null>(null);
  // PikPak 播放:點「播放」才查檔案列表;單支直接開播,多支列出讓使用者挑。
  const [files, setFiles] = useState<PresenceCodeFiles | null>(null);
  const [filesBusy, setFilesBusy] = useState(false);
  const [playing, setPlaying] = useState<{ id: string; name: string } | null>(
    null
  );

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
    refreshCloudCounts(() => alive);
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  function refreshCloudCounts(alive: () => boolean = () => true) {
    api
      .post<VideoCountResponse>("/api/pikpak/files/video-count", {
        items: [
          { key: "pikpak", code },
          { key: "pcloud", code, provider: "pcloud" },
        ],
      })
      .then((r) => {
        if (!alive()) return;
        for (const res of r.results) {
          if (!res.ok) continue;
          if (res.key === "pikpak") setCloudCount(res);
          else if (res.key === "pcloud") setPcloudCount(res);
        }
      })
      .catch(() => {});
  }

  // After a live finalize the files were renamed / junk removed — the
  // cached play list and cloud counts are stale.
  function onFinalized() {
    setFiles(null);
    refreshCloudCounts();
  }

  async function loadFiles() {
    if (filesBusy) return;
    if (files) {
      if (files.files.length === 1) setPlaying(files.files[0]);
      return;
    }
    setFilesBusy(true);
    try {
      const res = await api.get<PresenceCodeFiles>(
        `/api/pikpak/presence/codes/${encodeURIComponent(code)}/files`
      );
      setFiles(res);
      if (res.files.length === 1) setPlaying(res.files[0]);
      else if (res.files.length === 0) toast.error("PikPak 上找不到影片檔");
    } catch (e: any) {
      toast.error(e.message || "查詢影片失敗");
    } finally {
      setFilesBusy(false);
    }
  }

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
            {!((cloudCount?.video_count ?? 0) > 0 ||
               (pcloudCount?.video_count ?? 0) > 0) &&
              data.part_estimate &&
              data.part_estimate.likely !== "unknown" && (
                <>
                  <dt className="text-muted-foreground">分集(估計)</dt>
                  <dd>
                    <PartEstimateLabel est={data.part_estimate} />
                  </dd>
                </>
              )}
            {(cloudCount || pcloudCount) && (
              <>
                <dt className="text-muted-foreground">雲端影片</dt>
                <dd className="space-y-1">
                  <div className="space-x-2">
                    {cloudCount && (
                      <CloudCountLabel label="PikPak" result={cloudCount} />
                    )}
                    {cloudCount && cloudCount.video_count > 0 && (
                      <button
                        onClick={loadFiles}
                        disabled={filesBusy}
                        className="inline-flex items-center gap-1 text-xs text-emerald-300 hover:underline disabled:opacity-40"
                      >
                        <Play className="h-3 w-3" aria-hidden />
                        {filesBusy ? "查詢中…" : "播放"}
                      </button>
                    )}
                    {cloudCount && cloudCount.video_count > 0 && (
                      <FinalizeButton code={code} onDone={onFinalized} />
                    )}
                    {pcloudCount && (
                      <CloudCountLabel label="pCloud" result={pcloudCount} />
                    )}
                  </div>
                  {files && files.files.length > 1 && (
                    <div className="space-y-0.5 text-xs">
                      {files.files.map((f) => (
                        <div key={f.id} className="flex items-center gap-2">
                          <button
                            onClick={() => setPlaying(f)}
                            className="inline-flex shrink-0 items-center gap-1 text-emerald-300 hover:underline"
                          >
                            <Play className="h-3 w-3" aria-hidden />
                            播放
                          </button>
                          <span
                            className="truncate font-mono text-foreground/70"
                            title={f.path || f.name}
                          >
                            {f.name}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </dd>
              </>
            )}
          </dl>

          {/* 製作資訊 — identity metadata grouped in one place */}
          <div className="rounded-md border border-border bg-muted/20 p-3">
            <div className="mb-2 text-xs font-medium text-muted-foreground">
              製作資訊
            </div>
            <div className="grid grid-cols-1 gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
              <MetaItem icon={Building2} k="製作商" kind="studio" refv={data.studio} />
              <MetaItem icon={Tag} k="發行商" kind="label" refv={data.label} />
              <MetaItem icon={Clapperboard} k="系列" kind="series" refv={data.series} />
              <MetaItem icon={User} k="導演" kind="director" refv={data.director} />
              {data.release_date && (
                <div className="flex items-center gap-2 text-foreground/80">
                  <CalendarDays className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
                  <span className="text-muted-foreground">發行日期</span>
                  <span>{data.release_date}</span>
                </div>
              )}
              {data.duration && (
                <div className="flex items-center gap-2 text-foreground/80">
                  <Clock className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
                  <span className="text-muted-foreground">長度</span>
                  <span>{data.duration}</span>
                </div>
              )}
            </div>
            {!!data.genres.length && (
              <div className="mt-2">
                <div className="mb-1 text-xs text-muted-foreground">類別</div>
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
              </div>
            )}
          </div>
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

      <section className="rounded-lg border border-border">
        <button
          type="button"
          onClick={() => setMagnetsOpen((o) => !o)}
          aria-expanded={magnetsOpen}
          className="flex w-full items-center gap-2 px-4 py-3 text-left text-lg font-semibold transition hover:bg-muted/40"
        >
          {magnetsOpen ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" aria-hidden />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" aria-hidden />
          )}
          磁力連結
          <span className="text-sm font-normal text-muted-foreground">
            ({data.magnets.length})
          </span>
        </button>
        {magnetsOpen && (
          <div className="border-t border-border p-4">
            <MagnetTable
              magnets={data.magnets}
              code={data.code}
              sentHashes={sentHashes}
            />
          </div>
        )}
      </section>

      <VideoPlayerModal
        open={playing !== null}
        file={playing}
        onClose={() => setPlaying(null)}
      />
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

function MetaItem({
  icon: Icon,
  k,
  kind,
  refv,
}: {
  icon: LucideIcon;
  k: string;
  kind: "studio" | "label" | "series" | "director";
  refv: { name: string; id: string } | null;
}) {
  if (!refv || !refv.name) return null;
  return (
    <div className="flex items-center gap-2 text-foreground/80">
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
      <span className="text-muted-foreground">{k}</span>
      {refv.id ? (
        <Link
          href={`/${kind}/${encodeURIComponent(refv.id)}`}
          className="truncate hover:text-primary hover:underline"
        >
          {refv.name}
        </Link>
      ) : (
        <span className="truncate">{refv.name}</span>
      )}
    </div>
  );
}

function PartEstimateLabel({ est }: { est: PartEstimate }) {
  // Pre-download heuristic guess — deliberately styled cool/muted so it
  // is never confused with the authoritative amber CloudCountLabel.
  return (
    <span title={est.reason || undefined}>
      {est.likely === "multi" ? (
        <span className="text-sky-300">可能分集(估計)</span>
      ) : (
        <span className="text-foreground/60">可能單片(估計)</span>
      )}
    </span>
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
