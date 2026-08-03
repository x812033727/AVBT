"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Layers, Play } from "lucide-react";
import { confirmDialog, toast } from "@/components/Toast";
import VideoPlayerModal from "@/components/VideoPlayerModal";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type PresenceCodeFiles, type PresenceFileItem } from "@/lib/api";
import { fmtBytes } from "@/lib/format";

type MpFile = { path: string; name: string; has_marker: boolean };
type MpGroup = { parent: string; files: MpFile[] };
type MpReview = { status: string; note: string; updated_at: string | null };
type MpItem = {
  code: string;
  file_count: number;
  groups: MpGroup[];
  verdict_hint: "likely_parts" | "likely_dup_homes" | "mixed";
  review: MpReview | null;
};
type MpNeedsListing = { code: string; paths: string[]; review: MpReview | null };
type MpScan = {
  total_rows: number;
  items: MpItem[];
  needs_listing: MpNeedsListing[];
};

type Tab = "pending" | "confirmed_parts" | "resolved_dup" | "all";

const TAB_LABELS: { key: Tab; label: string }[] = [
  { key: "pending", label: "待審" },
  { key: "confirmed_parts", label: "已確認多集" },
  { key: "resolved_dup", label: "已解決" },
  { key: "all", label: "全部" },
];

const HINT_BADGE: Record<MpItem["verdict_hint"], { label: string; cls: string }> = {
  likely_parts: {
    label: "同夾多檔・可能是多集",
    cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  },
  likely_dup_homes: {
    label: "異夾複本・可能重複",
    cls: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  },
  mixed: {
    label: "混合・需人工",
    cls: "border-purple-500/40 bg-purple-500/10 text-purple-300",
  },
};

export default function MultipartPage() {
  const [data, setData] = useState<MpScan | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("pending");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Per-code live file lists (id + size), fetched on first expand.
  const [files, setFiles] = useState<Record<string, PresenceFileItem[]>>({});
  const [filesLoading, setFilesLoading] = useState<Set<string>>(new Set());
  // Selected file entries, keyed "code:fileId".
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [playing, setPlaying] = useState<PresenceFileItem | null>(null);

  function load() {
    setLoading(true);
    setError(null);
    api
      .get<MpScan>("/api/pikpak/multi-part")
      .then((d) => setData(d))
      .catch((e: any) => setError(e.message || "載入失敗"))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  const shown = useMemo(() => {
    if (!data) return [];
    if (tab === "all") return data.items;
    if (tab === "pending") return data.items.filter((i) => !i.review);
    return data.items.filter((i) => i.review?.status === tab);
  }, [data, tab]);

  const tabCounts = useMemo(() => {
    const c: Record<Tab, number> = {
      pending: 0,
      confirmed_parts: 0,
      resolved_dup: 0,
      all: data?.items.length ?? 0,
    };
    for (const i of data?.items ?? []) {
      if (!i.review) c.pending++;
      else if (i.review.status === "confirmed_parts") c.confirmed_parts++;
      else if (i.review.status === "resolved_dup") c.resolved_dup++;
    }
    return c;
  }, [data]);

  function toggleExpand(code: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
    if (!files[code] && !filesLoading.has(code)) {
      setFilesLoading((prev) => new Set(prev).add(code));
      api
        .get<PresenceCodeFiles>(
          `/api/pikpak/presence/codes/${encodeURIComponent(code)}/files`
        )
        .then((r) => setFiles((f) => ({ ...f, [code]: r.files })))
        .catch((e: any) => toast.error(`讀取 ${code} 檔案失敗:${e.message}`))
        .finally(() =>
          setFilesLoading((prev) => {
            const next = new Set(prev);
            next.delete(code);
            return next;
          })
        );
    }
  }

  async function review(code: string, status: string) {
    try {
      const out = await api.post<{ code: string; review: MpReview | null }>(
        `/api/pikpak/multi-part/${encodeURIComponent(code)}/review`,
        { status }
      );
      setData((d) =>
        d
          ? {
              ...d,
              items: d.items.map((i) =>
                i.code === code ? { ...i, review: out.review } : i
              ),
              needs_listing: d.needs_listing.map((n) =>
                n.code === code ? { ...n, review: out.review } : n
              ),
            }
          : d
      );
      toast.success(
        status === "pending" ? `${code} 已撤銷裁決` : `${code} 已標記`
      );
    } catch (e: any) {
      toast.error(`標記失敗:${e.message}`);
    }
  }

  function toggleFile(code: string, id: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      const key = `${code}:${id}`;
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  async function trashSelected() {
    const picks = Array.from(selected).map((k) => {
      const idx = k.indexOf(":");
      return { code: k.slice(0, idx), id: k.slice(idx + 1) };
    });
    if (!picks.length) return;
    const codes = Array.from(new Set(picks.map((p) => p.code)));
    const ok = await confirmDialog(
      `把選取的 ${picks.length} 個檔案移到 PikPak 垃圾桶?\n(涉及 ${codes.join("、")};只進垃圾桶,可復原)`
    );
    if (!ok) return;
    setBusy(true);
    try {
      await api.post("/api/pikpak/files/trash", {
        ids: picks.map((p) => p.id),
      });
      // Hand-trashed files leave the presence rows stale forever unless
      // the touched codes get re-read — same rule as every manual fix.
      for (let i = 0; i < codes.length; i += 50) {
        await api.post("/api/pikpak/presence/refresh-codes", {
          codes: codes.slice(i, i + 50),
        });
      }
      toast.success(`已把 ${picks.length} 個檔案移到垃圾桶`);
      const removed = new Set(picks.map((p) => `${p.code}:${p.id}`));
      setFiles((f) => {
        const next = { ...f };
        for (const code of codes) {
          if (next[code])
            next[code] = next[code].filter(
              (x) => !removed.has(`${code}:${x.id}`)
            );
        }
        return next;
      });
      setSelected(new Set());
      load();
    } catch (e: any) {
      toast.error(`刪除失敗:${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">多集管理</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          列出封存索引中同一番號持有多個檔案的條目。同一資料夾的{" "}
          <span className="font-mono text-xs">_1/_2</span>{" "}
          通常是真多集;不同資料夾的同名複本、或大小完全相同的成對檔,多半是重複下載。
          展開列可看每個檔案的實際大小(相同大小的配對會標紅),先播放確認再勾選刪除。
        </p>
      </div>

      {error && <ErrorBox message={error} onRetry={load} />}

      {loading && !data ? (
        <div className="space-y-2">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : data ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            {TAB_LABELS.map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={`rounded-md border px-3 py-1.5 text-sm transition ${
                  tab === key
                    ? "border-primary/50 bg-primary/10 text-primary"
                    : "border-border text-muted-foreground hover:bg-muted hover:text-foreground"
                }`}
              >
                {label}
                <span className="ml-1.5 text-xs opacity-70">
                  {tabCounts[key]}
                </span>
              </button>
            ))}
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto"
              onClick={load}
              disabled={loading}
            >
              重新整理
            </Button>
          </div>

          {shown.length === 0 ? (
            <EmptyState icon={Layers} title="這個分類目前沒有條目" />
          ) : (
            <div className="space-y-2">
              {shown.map((item) => (
                <CodeRow
                  key={item.code}
                  item={item}
                  expanded={expanded.has(item.code)}
                  files={files[item.code]}
                  filesLoading={filesLoading.has(item.code)}
                  selected={selected}
                  onExpand={() => toggleExpand(item.code)}
                  onToggleFile={toggleFile}
                  onReview={review}
                  onPlay={setPlaying}
                />
              ))}
            </div>
          )}

          {data.needs_listing.length > 0 && (
            <div className="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
              <div className="text-sm font-medium text-amber-200">
                資料夾型條目({data.needs_listing.length})—
                集數藏在資料夾內,索引看不到,需展開實際列出
              </div>
              <ul className="space-y-1">
                {data.needs_listing.map((n) => (
                  <li key={n.code} className="text-xs text-muted-foreground">
                    <span className="mr-2 font-mono font-medium text-foreground/80">
                      {n.code}
                    </span>
                    <span className="break-all font-mono">
                      {n.paths.join("、")}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      ) : null}

      {selected.size > 0 && (
        <div className="sticky bottom-3 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card/95 px-4 py-3 shadow-lg backdrop-blur">
          <span className="text-sm text-foreground/70">
            已選 {selected.size} 個檔案
          </span>
          <Button
            variant="outline"
            size="sm"
            className="border-red-500/40 bg-red-500/10 text-red-300 hover:bg-red-500/20 hover:text-red-300"
            onClick={trashSelected}
            disabled={busy}
          >
            移到垃圾桶
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setSelected(new Set())}
            disabled={busy}
          >
            清除選取
          </Button>
        </div>
      )}

      <VideoPlayerModal
        open={playing !== null}
        file={playing}
        onClose={() => setPlaying(null)}
      />
    </div>
  );
}

function CodeRow({
  item,
  expanded,
  files,
  filesLoading,
  selected,
  onExpand,
  onToggleFile,
  onReview,
  onPlay,
}: {
  item: MpItem;
  expanded: boolean;
  files: PresenceFileItem[] | undefined;
  filesLoading: boolean;
  selected: Set<string>;
  onExpand: () => void;
  onToggleFile: (code: string, id: string, on: boolean) => void;
  onReview: (code: string, status: string) => void;
  onPlay: (f: PresenceFileItem) => void;
}) {
  const hint = HINT_BADGE[item.verdict_hint];
  const reviewed = item.review?.status;
  // Byte-identical siblings are the #145 dupe signature — flag every
  // size that appears on more than one file of this code.
  const dupSizes = useMemo(() => {
    const seen = new Map<number, number>();
    for (const f of files ?? []) {
      if (f.size) seen.set(f.size, (seen.get(f.size) ?? 0) + 1);
    }
    return new Set(
      Array.from(seen.entries())
        .filter(([, n]) => n > 1)
        .map(([s]) => s)
    );
  }, [files]);

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <button
          onClick={onExpand}
          className="flex items-center gap-2 text-left"
          aria-expanded={expanded}
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          <span className="font-mono font-medium text-foreground/90">
            {item.code}
          </span>
          <span className="text-xs text-muted-foreground">
            {item.file_count} 檔・{item.groups.length} 個資料夾
          </span>
        </button>
        <span
          className={`rounded-full border px-2 py-0.5 text-[11px] ${hint.cls}`}
        >
          {hint.label}
        </span>
        {reviewed === "confirmed_parts" && (
          <span className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-300">
            已確認多集
          </span>
        )}
        {reviewed === "resolved_dup" && (
          <span className="rounded-full border border-sky-500/40 bg-sky-500/10 px-2 py-0.5 text-[11px] text-sky-300">
            已解決
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          {reviewed ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onReview(item.code, "pending")}
            >
              撤銷裁決
            </Button>
          ) : (
            <>
              <Button
                variant="outline"
                size="sm"
                className="border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10 hover:text-emerald-300"
                onClick={() => onReview(item.code, "confirmed_parts")}
              >
                確認多集
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onReview(item.code, "resolved_dup")}
              >
                標記已解決
              </Button>
            </>
          )}
        </div>
      </div>

      {expanded && (
        <div className="space-y-2 border-t border-border px-3 py-2">
          {item.groups.map((g) => (
            <div key={g.parent} className="text-xs text-muted-foreground">
              <div className="break-all font-mono opacity-70">{g.parent}/</div>
            </div>
          ))}
          {filesLoading ? (
            <Skeleton className="h-16 w-full" />
          ) : files && files.length > 0 ? (
            <ul className="space-y-1">
              {files.map((f) => {
                const key = `${item.code}:${f.id}`;
                const isDupSize = !!f.size && dupSizes.has(f.size);
                return (
                  <li key={key} className="flex items-center gap-2">
                    <Checkbox
                      checked={selected.has(key)}
                      onCheckedChange={(v) =>
                        onToggleFile(item.code, f.id, v === true)
                      }
                      aria-label={`選取 ${f.name}`}
                    />
                    <span className="break-all font-mono text-xs text-foreground/80">
                      {f.name}
                    </span>
                    <span
                      className={`ml-auto shrink-0 font-mono text-xs ${
                        isDupSize
                          ? "rounded bg-red-500/15 px-1.5 py-0.5 font-medium text-red-300"
                          : "text-muted-foreground"
                      }`}
                      title={isDupSize ? "與另一檔大小完全相同 — 重複的典型特徵" : undefined}
                    >
                      {fmtBytes(f.size, "大小不明")}
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2"
                      onClick={() => onPlay(f)}
                    >
                      <Play className="h-3.5 w-3.5" />
                    </Button>
                  </li>
                );
              })}
            </ul>
          ) : (
            <div className="text-xs text-muted-foreground">
              沒有讀到可播放的檔案(索引可能過期,可到設定頁重讀該番號)
            </div>
          )}
        </div>
      )}
    </div>
  );
}
