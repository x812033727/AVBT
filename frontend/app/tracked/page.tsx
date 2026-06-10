"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import BatchScanModal from "@/components/BatchScanModal";
import { confirmDialog, toast } from "@/components/Toast";
import {
  TRACKED_LABELS,
  api,
  imgProxy,
  streamNdjson,
  type CheckListingResult,
  type MissingCodesResult,
  type MissingSummary,
  type MissingSummaryItem,
  type MovieListItem,
  type PresenceCodeLookup,
  type TrackedKind,
  type TrackedListing,
  type TrackerStatus,
} from "@/lib/api";

function fmt(d: string | null): string {
  if (!d) return "從未檢查";
  return new Date(d.endsWith("Z") ? d : d + "Z").toLocaleString();
}

const KIND_FILTERS: { value: TrackedKind | ""; label: string }[] = [
  { value: "", label: "全部" },
  { value: "star", label: "女優" },
  { value: "studio", label: "製作商" },
  { value: "label", label: "發行商" },
  { value: "series", label: "系列" },
  { value: "director", label: "導演" },
];

const KIND_COLORS: Record<TrackedKind, string> = {
  star: "bg-pink-500/20 text-pink-300",
  studio: "bg-blue-500/20 text-blue-300",
  label: "bg-violet-500/20 text-violet-300",
  series: "bg-emerald-500/20 text-emerald-300",
  director: "bg-amber-500/20 text-amber-300",
};

const ADD_KINDS: { value: TrackedKind; label: string }[] = [
  { value: "star", label: "女優" },
  { value: "studio", label: "製作商" },
  { value: "label", label: "發行商" },
  { value: "series", label: "系列" },
  { value: "director", label: "導演" },
];

export default function TrackedPage() {
  const [items, setItems] = useState<TrackedListing[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [checkingKey, setCheckingKey] = useState<string | null>(null);
  // Per-row phase label shown next to "立即檢查" while streaming (e.g.
  // "page 1…" → "掃描缺漏…" → "完成"). Keyed by `${kind}:${id}`.
  const [checkingPhase, setCheckingPhase] = useState<string>("");
  const [lastCheck, setLastCheck] = useState<CheckListingResult | null>(null);
  const [filter, setFilter] = useState<TrackedKind | "">("");
  const [missing, setMissing] = useState<Map<string, MissingSummaryItem> | null>(
    null
  );
  const [missingLoading, setMissingLoading] = useState(false);
  // Batch-scan modal: either "check-all" or "missing-summary".
  const [batchModalMode, setBatchModalMode] = useState<
    "check-all" | "missing-summary" | null
  >(null);
  // Live tracker status (polled while the page is mounted) — used to
  // show an inline banner "背景掃描中 X/Y: <listing>" when the periodic
  // run_loop is running. The streaming buttons have their own modal so
  // this banner is purely for background-tick visibility.
  const [trackerStatus, setTrackerStatus] = useState<TrackerStatus | null>(
    null,
  );
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [details, setDetails] = useState<Map<string, MissingCodesResult>>(
    new Map()
  );
  const [detailLoading, setDetailLoading] = useState<Set<string>>(new Set());
  const [lookups, setLookups] = useState<Map<string, PresenceCodeLookup>>(
    new Map()
  );
  const [lookupBusy, setLookupBusy] = useState<Set<string>>(new Set());

  // Manual-add form state
  const [addKind, setAddKind] = useState<TrackedKind>("studio");
  const [addSlug, setAddSlug] = useState("");
  const [addAuto, setAddAuto] = useState(false);
  const [addBusy, setAddBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.get<TrackedListing[]>("/api/tracked");
      setItems(res);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const loadMissing = useCallback(async (refresh = false) => {
    setMissingLoading(true);
    try {
      const qs = refresh ? "?refresh=true" : "";
      const res = await api.get<MissingSummary>(
        `/api/tracked/missing-summary${qs}`
      );
      const m = new Map<string, MissingSummaryItem>();
      for (const it of res.items) m.set(`${it.kind}:${it.id}`, it);
      setMissing(m);
    } catch {
      setMissing(null);
    } finally {
      setMissingLoading(false);
    }
  }, []);

  // Shared row-patch helper: every per-listing ``done`` / ``progress``
  // event from the streaming endpoints carries enough state
  // (last_checked_at, last_seen_code, new_count, last_error,
  // missing_count) to update the row inline without re-fetching
  // /api/tracked. Used by the single 立即檢查 done callback AND by the
  // batch modal's onProgress, so rows feel live across all three
  // streaming flows.
  const patchRowFromEvent = useCallback((event: any) => {
    if (!event?.kind || !event?.id) return;
    const key = `${event.kind}:${event.id}`;

    setItems((prev) =>
      prev.map((it) => {
        if (it.kind !== event.kind || it.id !== event.id) return it;
        const next = { ...it };
        if (event.last_checked_at !== undefined)
          next.last_checked_at = event.last_checked_at;
        if (event.last_seen_code !== undefined)
          next.last_seen_code = event.last_seen_code;
        if (event.new_count !== undefined)
          next.new_count = event.new_count;
        if (event.last_error !== undefined)
          next.last_error = event.last_error;
        return next;
      }),
    );

    if (typeof event.missing_count === "number") {
      setMissing((prev) => {
        const next = new Map(prev ?? []);
        const existing = next.get(key);
        if (existing) {
          next.set(key, { ...existing, missing_count: event.missing_count });
        }
        return next;
      });
    }
  }, []);

  useEffect(() => {
    loadMissing(false);
  }, [loadMissing]);

  // Poll tracker status so the inline progress banner can show the
  // background loop's progress (`scan_in_progress` flips on/off and
  // `scan_current` / `scan_total` advance). Polls more frequently while
  // a scan is in flight so the X/Y counter feels live.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    async function poll() {
      try {
        const res = await api.get<TrackerStatus>("/api/tracked/status");
        if (cancelled) return;
        setTrackerStatus(res);
        // 2s while scanning so the X/Y counter looks live, otherwise 15s
        // so we're not hammering the backend with idle polls.
        const next = res.scan_in_progress ? 2000 : 15000;
        timer = window.setTimeout(poll, next);
      } catch {
        if (cancelled) return;
        timer = window.setTimeout(poll, 15000);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  // Background tick has no streaming pipe to the UI, so during it we
  // poll the rows list every 4s. As each listing's check_listing_stream
  // commits last_checked_at / new_count / last_missing_count, the next
  // poll picks it up and the row refreshes inline alongside the banner.
  useEffect(() => {
    if (!trackerStatus?.scan_in_progress) return;
    const id = window.setInterval(load, 4000);
    return () => window.clearInterval(id);
  }, [trackerStatus?.scan_in_progress, load]);

  // When scan_in_progress flips true → false, the background loop just
  // invalidated _summary_result and presence — kick a single missing
  // summary fetch so the deduped badges catch up to whatever changed.
  // Skips on initial mount (prevScanRef starts false).
  const prevScanRef = useRef(false);
  useEffect(() => {
    const cur = trackerStatus?.scan_in_progress ?? false;
    if (prevScanRef.current && !cur) {
      loadMissing(false);
    }
    prevScanRef.current = cur;
  }, [trackerStatus?.scan_in_progress, loadMissing]);

  // Flip the periodic background-scan loop on/off (tracker.state.enabled).
  // The same toggle lives on /settings; surfaced here too so the user can
  // pause/resume scheduled scanning right where they manage tracked items.
  async function toggleBackgroundScan(enabled: boolean) {
    // Optimistic flip for instant feedback, then reconcile with the
    // server response; on failure revert and surface the error. The
    // status poll loop would also eventually correct a stale value.
    setTrackerStatus((prev) => (prev ? { ...prev, enabled } : prev));
    try {
      const res = await api.post<TrackerStatus>("/api/tracked/status/toggle", {
        enabled,
      });
      setTrackerStatus(res);
    } catch (e: any) {
      setTrackerStatus((prev) =>
        prev ? { ...prev, enabled: !enabled } : prev,
      );
      toast.error(e.message || "切換背景掃描失敗");
    }
  }

  function keyOf(it: TrackedListing) {
    return `${it.kind}:${it.id}`;
  }

  async function remove(it: TrackedListing) {
    const ok = await confirmDialog(
      `不再追蹤 ${TRACKED_LABELS[it.kind]} ${it.name || it.id}？`
    );
    if (!ok) return;
    try {
      await api.del(`/api/tracked/${it.kind}/${encodeURIComponent(it.id)}`);
      toast.success("已取消追蹤");
      load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function toggleAuto(it: TrackedListing) {
    await api.post("/api/tracked", { ...it, auto_send: !it.auto_send });
    load();
  }

  async function checkNow(it: TrackedListing) {
    const key = keyOf(it);
    setCheckingKey(key);
    setCheckingPhase("page 1…");
    setLastCheck(null);
    let finalResult: CheckListingResult | null = null;
    try {
      // Stream the per-phase events so the button label can update from
      // "page 1…" → "掃描缺漏…" → "完成" instead of just showing a static
      // "檢查中" while the catalog walk runs in the background.
      await streamNdjson(
        `/api/tracked/${it.kind}/${encodeURIComponent(it.id)}/check/stream`,
        {},
        (event) => {
          if (event.type === "progress") {
            const phase = event.phase as string | undefined;
            if (phase === "page1") setCheckingPhase("page 1…");
            else if (phase === "missing_scan") setCheckingPhase("掃描缺漏…");
            else if (phase === "enqueue") {
              const queued = Number(event.queued ?? 0);
              setCheckingPhase(queued > 0 ? `已送 ${queued}…` : "完成中…");
            }
          } else if (event.type === "done") {
            finalResult = {
              kind: event.kind,
              id: event.id,
              name: event.name ?? "",
              new_codes: event.new_codes ?? [],
              error: event.error ?? "",
            };
            const missingCount = event.missing_count;
            const errMsg = event.error;
            if (errMsg) {
              setCheckingPhase("失敗");
            } else if (typeof missingCount === "number") {
              setCheckingPhase(`完成 (${missingCount} 缺漏)`);
            } else {
              setCheckingPhase("完成");
            }
            // Patch the row inline from the done event's snapshot
            // (last_checked_at / last_seen_code / new_count / last_error
            // / missing_count) — no /api/tracked round-trip needed.
            // /missing-summary is intentionally NOT re-fetched because
            // the server-side cache was invalidated by /check/stream and
            // a fresh fetch would walk every listing's JavBus catalog
            // again (minutes).
            patchRowFromEvent(event);
          } else if (event.type === "error") {
            setCheckingPhase("失敗");
            setError(event.message ?? "未知錯誤");
          }
        },
      );
      if (finalResult) setLastCheck(finalResult);
      // Clear cached detail for this row so re-opening shows fresh data
      // (the row's missing-codes detail panel re-fetches on expand).
      setDetails((m) => {
        const next = new Map(m);
        next.delete(key);
        return next;
      });
    } catch (e: any) {
      if (e.name !== "AbortError") setError(e.message);
      setCheckingPhase("失敗");
    } finally {
      // Hold the final phase label briefly so the user can read it,
      // then clear so the button text returns to "立即檢查".
      setTimeout(() => {
        setCheckingKey(null);
        setCheckingPhase("");
      }, 1500);
    }
  }

  async function resetNew(it: TrackedListing) {
    await api.post(
      `/api/tracked/${it.kind}/${encodeURIComponent(it.id)}/reset-new-count`
    );
    load();
  }

  async function toggleExpand(it: TrackedListing) {
    const key = keyOf(it);
    const next = new Set(expanded);
    if (next.has(key)) {
      next.delete(key);
      setExpanded(next);
      return;
    }
    next.add(key);
    setExpanded(next);
    if (details.has(key)) return;
    setDetailLoading((s) => new Set(s).add(key));
    try {
      const res = await api.get<MissingCodesResult>(
        `/api/tracked/${it.kind}/${encodeURIComponent(it.id)}/missing-codes`
      );
      setDetails((m) => new Map(m).set(key, res));
    } catch (e: any) {
      toast.error(e.message || "讀取缺漏明細失敗");
    } finally {
      setDetailLoading((s) => {
        const n = new Set(s);
        n.delete(key);
        return n;
      });
    }
  }

  async function lookupCode(code: string) {
    if (lookupBusy.has(code) || lookups.has(code)) return;
    setLookupBusy((s) => new Set(s).add(code));
    try {
      const res = await api.get<PresenceCodeLookup>(
        `/api/pikpak/presence/codes/${encodeURIComponent(code)}`
      );
      setLookups((m) => new Map(m).set(code, res));
    } catch (e: any) {
      toast.error(e.message || "查詢失敗");
    } finally {
      setLookupBusy((s) => {
        const n = new Set(s);
        n.delete(code);
        return n;
      });
    }
  }

  function openBatchCheckAll() {
    // The actual streaming + UI happens in <BatchScanModal mode="check-all" />.
    // On done, we refresh local state (loadMissing + load).
    setLastCheck(null);
    setError(null);
    setBatchModalMode("check-all");
  }

  function openBatchMissingSummary() {
    setBatchModalMode("missing-summary");
  }

  function onBatchModalDone() {
    // Streamed work just finished — pull fresh aggregate data + rows so
    // the badges and last_seen / last_checked_at reflect the new state.
    setDetails(new Map());
    loadMissing(false);
    load();
  }

  async function manualAdd(e: React.FormEvent) {
    e.preventDefault();
    // Be forgiving: strip leading kind/ and surrounding slashes so the
    // user can paste either "ca" or "studio/ca" or "/studio/ca/" and
    // we end up with just "ca".
    let slug = addSlug.trim().replace(/^\/+|\/+$/g, "");
    if (slug.toLowerCase().startsWith(`${addKind.toLowerCase()}/`)) {
      slug = slug.slice(addKind.length + 1);
    }
    slug = slug.replace(/^\/+|\/+$/g, "");
    if (!slug) return;
    setAddBusy(true);
    setError(null);
    try {
      // name="" → backend tries to fetch the listing's real title.
      await api.post<TrackedListing>("/api/tracked", {
        kind: addKind,
        id: slug,
        name: "",
        avatar: "",
        uncensored: false,
        auto_send: addAuto,
      });
      setAddSlug("");
      setAddAuto(false);
      load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setAddBusy(false);
    }
  }

  const filtered = filter ? items.filter((i) => i.kind === filter) : items;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">追蹤中</h1>
        <span className="text-sm text-white/40">({filtered.length} / {items.length})</span>
        <div className="flex flex-wrap gap-1">
          {KIND_FILTERS.map((f) => (
            <button
              key={f.value}
              onClick={() => setFilter(f.value)}
              className={
                "rounded-md px-3 py-1 text-xs " +
                (filter === f.value
                  ? "bg-accent text-black"
                  : "border border-white/10 text-white/60 hover:bg-white/5")
              }
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-3">
          <label
            className="flex items-center gap-1.5 text-xs text-white/60"
            title={
              trackerStatus
                ? `開啟後每 ${trackerStatus.interval_seconds} 秒自動檢查所有追蹤項目的新作品 / 缺漏；關閉只停掉排程，手動「立即檢查」仍可用`
                : "背景排程掃描"
            }
          >
            <input
              type="checkbox"
              checked={trackerStatus?.enabled ?? false}
              onChange={(e) => toggleBackgroundScan(e.target.checked)}
              disabled={!trackerStatus}
            />
            背景掃描
          </label>
          {filtered.length > 0 && (
            <div className="flex gap-2">
              <button
                className="btn-ghost"
                onClick={openBatchMissingSummary}
                disabled={missingLoading || batchModalMode !== null}
                title="重新掃 PikPak 資料夾並重算缺漏"
              >
                重算缺漏
              </button>
              <button
                className="btn-ghost"
                onClick={openBatchCheckAll}
                disabled={!!checkingKey || batchModalMode !== null}
              >
                全部立即檢查
              </button>
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {trackerStatus?.scan_in_progress && (
        <div className="space-y-2 rounded-md border border-blue-500/30 bg-blue-500/10 px-3 py-2 text-sm text-blue-200">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>
              背景掃描中…{" "}
              {trackerStatus.scan_total > 0 && (
                <span className="font-mono">
                  {trackerStatus.scan_current} / {trackerStatus.scan_total}
                  {" "}
                  ({Math.round(
                    (trackerStatus.scan_current /
                      Math.max(trackerStatus.scan_total, 1)) *
                      100,
                  )}
                  %)
                </span>
              )}
            </span>
            {trackerStatus.scan_name && (
              <span className="truncate font-mono text-blue-300/80">
                {trackerStatus.scan_name}
              </span>
            )}
          </div>
          {trackerStatus.scan_total > 0 && (
            <div className="h-1.5 overflow-hidden rounded bg-blue-500/20">
              <div
                className="h-full bg-blue-400 transition-[width]"
                style={{
                  width: `${Math.round(
                    (trackerStatus.scan_current /
                      Math.max(trackerStatus.scan_total, 1)) *
                      100,
                  )}%`,
                }}
              />
            </div>
          )}
        </div>
      )}

      <form
        onSubmit={manualAdd}
        className="flex flex-wrap items-center gap-2 rounded-lg border border-white/10 bg-panel/50 px-3 py-2 text-sm"
      >
        <span className="text-xs text-white/60">手動新增</span>
        <select
          value={addKind}
          onChange={(e) => setAddKind(e.target.value as TrackedKind)}
          className="rounded-md border border-white/10 bg-ink px-2 py-1 text-xs"
        >
          {ADD_KINDS.map((k) => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </select>
        <input
          value={addSlug}
          onChange={(e) => setAddSlug(e.target.value)}
          placeholder="JavBus slug, 例如 studio/ca 的 ca"
          className="flex-1 min-w-[180px] rounded-md border border-white/10 bg-ink px-2 py-1 text-xs font-mono outline-none focus:border-accent"
        />
        <label className="flex items-center gap-1 text-xs text-white/60">
          <input
            type="checkbox"
            checked={addAuto}
            onChange={(e) => setAddAuto(e.target.checked)}
          />
          自動送 PikPak
        </label>
        <button
          type="submit"
          className="btn-primary disabled:opacity-50"
          disabled={addBusy || !addSlug.trim()}
        >
          {addBusy ? "新增中…" : "+ 追蹤"}
        </button>
      </form>

      {lastCheck && (
        <div
          className={
            "rounded-md border px-3 py-2 text-sm " +
            (lastCheck.error
              ? "border-red-500/30 bg-red-500/10 text-red-300"
              : lastCheck.new_codes.length
              ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
              : "border-white/10 bg-white/5 text-white/60")
          }
        >
          {lastCheck.error
            ? `${TRACKED_LABELS[lastCheck.kind]} ${lastCheck.name || lastCheck.id}: ${lastCheck.error}`
            : lastCheck.new_codes.length
            ? `${TRACKED_LABELS[lastCheck.kind]} ${lastCheck.name} 有 ${lastCheck.new_codes.length} 部新作品: ${lastCheck.new_codes.join(", ")}`
            : `${TRACKED_LABELS[lastCheck.kind]} ${lastCheck.name} 沒有新作品`}
        </div>
      )}

      {!filtered.length && (
        <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
          {filter ? (
            `沒有追蹤任何${TRACKED_LABELS[filter as TrackedKind]}`
          ) : (
            <>
              還沒追蹤任何東西。可在上方手動新增，或到對應頁面點「★ 追蹤」：
              <div className="mt-2 flex flex-wrap justify-center gap-2 text-xs">
                <code className="rounded bg-white/10 px-2 py-0.5">/star/{"{slug}"}</code>
                <code className="rounded bg-white/10 px-2 py-0.5">/studio/{"{slug}"}</code>
                <code className="rounded bg-white/10 px-2 py-0.5">/series/{"{slug}"}</code>
                <code className="rounded bg-white/10 px-2 py-0.5">/label/{"{slug}"}</code>
                <code className="rounded bg-white/10 px-2 py-0.5">/director/{"{slug}"}</code>
              </div>
            </>
          )}
        </div>
      )}

      <div className="grid gap-3">
        {filtered.map((it) => (
          <div
            key={keyOf(it)}
            className="rounded-lg border border-white/10 bg-panel"
          >
            <div className="flex flex-wrap gap-3 p-3">
            {it.avatar ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={imgProxy(it.avatar)}
                alt={it.name}
                referrerPolicy="no-referrer"
                className="h-20 w-16 flex-none rounded object-cover"
              />
            ) : (
              <div className="grid h-20 w-16 flex-none place-items-center rounded bg-white/5 text-xl text-white/30">
                {it.kind === "star" ? "?" : "📁"}
              </div>
            )}
            <div className="min-w-0 flex-1 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={
                    "rounded px-2 py-0.5 text-xs " + KIND_COLORS[it.kind]
                  }
                >
                  {TRACKED_LABELS[it.kind]}
                </span>
                <Link
                  href={`/${it.kind}/${encodeURIComponent(it.id)}`}
                  className="font-semibold text-accent hover:underline"
                >
                  {it.name || it.id}
                </Link>
                {it.new_count > 0 && (
                  <button
                    onClick={() => resetNew(it)}
                    className="rounded bg-amber-500/20 px-2 py-0.5 text-xs text-amber-300 hover:bg-amber-500/30"
                    title="點擊清除"
                  >
                    {it.new_count} 新
                  </button>
                )}
                {(() => {
                  const m = missing?.get(keyOf(it));
                  if (missingLoading && !m) {
                    return (
                      <span className="rounded bg-white/5 px-2 py-0.5 text-xs text-white/30">
                        缺漏…
                      </span>
                    );
                  }
                  if (!m) return null;
                  if (m.error) {
                    return (
                      <span
                        className="rounded bg-red-500/15 px-2 py-0.5 text-xs text-red-300"
                        title={m.error}
                      >
                        缺漏 ?
                      </span>
                    );
                  }
                  const missingBadge =
                    m.total === 0 ? (
                      <span
                        key="no-listing"
                        className="rounded bg-white/10 px-2 py-0.5 text-xs text-white/40"
                        title="JavBus 沒回傳列表(可能 slug 失效 / 網路 / 地區封鎖),所以無法判斷缺漏 / 多餘"
                      >
                        未取得列表
                      </span>
                    ) : m.missing_count > 0 ? (
                      <button
                        key="missing"
                        onClick={() => toggleExpand(it)}
                        className="rounded bg-amber-400/20 px-2 py-0.5 text-xs text-amber-300 hover:bg-amber-400/30"
                        title={`全集 ${m.total} 部,掃 ${m.pages_scanned} 頁 · 點擊看明細`}
                      >
                        {m.missing_count} 個未下載{" "}
                        {expanded.has(keyOf(it)) ? "▾" : "▸"}
                      </button>
                    ) : (
                      <span
                        key="all-here"
                        className="rounded bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-300"
                        title={`全集 ${m.total} 部都已下載`}
                      >
                        全收齊
                      </span>
                    );
                  const extrasBadge =
                    m.extras_count > 0 ? (
                      <button
                        key="extras"
                        onClick={() => toggleExpand(it)}
                        className="rounded bg-purple-500/20 px-2 py-0.5 text-xs text-purple-300 hover:bg-purple-500/30"
                        title="此資料夾裡有 JavBus 列表沒有的番號 · 點擊看明細"
                      >
                        {m.extras_count} 多餘{" "}
                        {expanded.has(keyOf(it)) ? "▾" : "▸"}
                      </button>
                    ) : null;
                  return (
                    <>
                      {missingBadge}
                      {extrasBadge}
                    </>
                  );
                })()}
                {it.uncensored && <span className="tag">無碼</span>}
              </div>
              <div className="text-xs text-white/40">
                slug: <span className="font-mono">{it.id}</span>
                {it.last_seen_code && (
                  <>
                    {" · 最後看到: "}
                    <span className="font-mono">{it.last_seen_code}</span>
                  </>
                )}
              </div>
              <div className="text-xs text-white/40">
                最後檢查 {fmt(it.last_checked_at)}
              </div>
              {it.last_error && (
                <div className="line-clamp-2 text-xs text-amber-300/80">
                  ⚠ {it.last_error}
                </div>
              )}
            </div>
            <div className="flex flex-col items-end gap-1 text-xs">
              <label
                className="flex items-center gap-1"
                title="開啟後,排程檢查時會把 JavBus 列表上、PikPak 還沒有的番號都自動送上去(不只新作品),已送過的會用 btih 去重"
              >
                <input
                  type="checkbox"
                  checked={it.auto_send}
                  onChange={() => toggleAuto(it)}
                />
                自動送 PikPak
              </label>
              <div className="flex gap-2">
                <button
                  onClick={() => checkNow(it)}
                  disabled={!!checkingKey || batchModalMode !== null}
                  className="text-blue-300 hover:underline disabled:opacity-50"
                  title={
                    batchModalMode
                      ? "批次掃描中"
                      : checkingKey === keyOf(it)
                      ? checkingPhase || "檢查中"
                      : undefined
                  }
                >
                  {checkingKey === keyOf(it)
                    ? checkingPhase || "檢查中"
                    : "立即檢查"}
                </button>
                <button
                  onClick={() => remove(it)}
                  className="text-red-300 hover:underline"
                >
                  取消追蹤
                </button>
              </div>
            </div>
            </div>

            {expanded.has(keyOf(it)) && (
              <MissingDetailPanel
                tracked={it}
                detail={details.get(keyOf(it)) || null}
                loading={detailLoading.has(keyOf(it))}
                lookups={lookups}
                lookupBusy={lookupBusy}
                onLookup={lookupCode}
              />
            )}
          </div>
        ))}
      </div>

      <BatchScanModal
        open={batchModalMode !== null}
        mode={batchModalMode ?? "check-all"}
        onClose={() => setBatchModalMode(null)}
        onDone={onBatchModalDone}
        onProgress={patchRowFromEvent}
      />
    </div>
  );
}

function MissingDetailPanel({
  tracked,
  detail,
  loading,
  lookups,
  lookupBusy,
  onLookup,
}: {
  tracked: TrackedListing;
  detail: MissingCodesResult | null;
  loading: boolean;
  lookups: Map<string, PresenceCodeLookup>;
  lookupBusy: Set<string>;
  onLookup: (code: string) => void;
}) {
  function expectedPath(code: string) {
    // expected_root comes from the backend so it matches the actual
    // archiver sanitization (preserves kana, spaces, etc.).
    return `${detail?.expected_root || `AVBT/${tracked.kind}/${tracked.name || tracked.id}`}/${code}`;
  }
  if (loading && !detail) {
    return (
      <div className="border-t border-white/10 px-4 py-3 text-xs text-white/50">
        讀取缺漏明細中…
      </div>
    );
  }
  if (!detail) return null;
  if (!detail.missing.length && !detail.extras.length) {
    return (
      <div className="border-t border-white/10 px-4 py-3 text-xs text-emerald-300/80">
        ✓ 已無缺漏、也沒有多餘番號
      </div>
    );
  }
  return (
    <div className="border-t border-white/10 px-4 py-3 text-xs">
      <div className="mb-2 space-y-1">
        <div className="text-white/60">
          共 <span className="font-mono">{detail.total}</span> 部・已下載{" "}
          <span className="font-mono text-emerald-300/80">
            {detail.present_codes.length}
          </span>
          ・缺漏{" "}
          <span className="font-mono text-amber-300">
            {detail.missing.length}
          </span>
          {detail.extras.length > 0 && (
            <>
              ・多餘{" "}
              <span className="font-mono text-purple-300">
                {detail.extras.length}
              </span>
            </>
          )}
          <span className="ml-2 text-white/40">
            (掃 {detail.pages_scanned} 頁)
          </span>
        </div>
        <div className="text-white/40">
          判斷路徑:
          <span className="ml-1 font-mono text-white/70">
            {detail.expected_root}/&lt;番號&gt;
          </span>
        </div>
      </div>
      <ul className="divide-y divide-white/5">
        {detail.missing.map((m: MovieListItem) => {
          const lookup = lookups.get(m.code);
          const busy = lookupBusy.has(m.code);
          return (
            <li key={m.code} className="py-2">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <Link
                  href={`/movie/${encodeURIComponent(m.code)}`}
                  className="font-mono text-accent hover:underline"
                >
                  {m.code}
                </Link>
                <span className="truncate text-white/70" title={m.title}>
                  {m.title}
                </span>
                <span className="ml-auto flex items-center gap-2">
                  <span
                    className="font-mono text-white/40"
                    title="archiver 預期會放這裡"
                  >
                    {expectedPath(m.code)}
                  </span>
                  <button
                    onClick={() => onLookup(m.code)}
                    disabled={busy || lookup !== undefined}
                    className="text-blue-300 hover:underline disabled:opacity-40"
                  >
                    {busy
                      ? "查詢中…"
                      : lookup !== undefined
                      ? "已查詢"
                      : "查實際位置"}
                  </button>
                </span>
              </div>
              {lookup && (
                <div className="mt-1 pl-2 text-[11px]">
                  {lookup.paths.length === 0 ? (
                    <span className="text-amber-300/80">
                      ⚠ 索引裡找不到此番號(或在未被掃描的路徑下)
                    </span>
                  ) : (
                    <div className="space-y-0.5">
                      <span className="text-emerald-300/80">
                        ✓ 實際在以下路徑找到({lookup.paths.length}):
                      </span>
                      {lookup.paths.map((p) => (
                        <div
                          key={p}
                          className="font-mono text-emerald-200/80"
                        >
                          ・{p}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {detail.extras.length > 0 && (
        <div className="mt-3 border-t border-white/5 pt-2">
          <div className="mb-1 text-white/60">
            多餘番號 ({detail.extras.length})
            <span className="ml-1 text-white/40">
              · 此資料夾裡有,但不在 JavBus 列表內
            </span>
          </div>
          <ul className="divide-y divide-white/5">
            {detail.extras.map((e) => (
              <li key={e.code} className="py-1.5">
                <div className="flex items-center gap-2">
                  <Link
                    href={`/movie/${encodeURIComponent(e.code)}`}
                    className="font-mono text-purple-300 hover:underline"
                  >
                    {e.code}
                  </Link>
                </div>
                {e.paths.map((p) => (
                  <div
                    key={p}
                    className="pl-2 font-mono text-[11px] text-white/50"
                  >
                    ・{p}
                  </div>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
