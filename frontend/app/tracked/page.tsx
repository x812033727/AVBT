"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import BatchScanModal from "@/components/BatchScanModal";
import { toast } from "@/components/Toast";
import VideoPlayerModal from "@/components/VideoPlayerModal";
import { ErrorBox } from "@/components/shared/ErrorBox";
import AddTrackedForm from "@/components/tracked/AddTrackedForm";
import TrackedList from "@/components/tracked/TrackedList";
import TrackedToolbar from "@/components/tracked/TrackedToolbar";
import TrackerStatusBar from "@/components/tracked/TrackerStatusBar";
import { keyOf } from "@/components/tracked/TrackedRow";
import {
  TRACKED_LABELS,
  api,
  streamNdjson,
  type CheckListingResult,
  type MissingCodesResult,
  type MissingSummary,
  type MissingSummaryItem,
  type PresenceCodeFiles,
  type PresenceCodeLookup,
  type TrackedKind,
  type TrackedListing,
  type TrackerStatus,
} from "@/lib/api";

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
  // Non-fatal summary refresh failure: keep showing the last good map
  // and surface a small notice instead of blanking every badge.
  const [missingError, setMissingError] = useState<string | null>(null);
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
  // 番號 → PikPak 影片檔(播放用),與正在查詢中的番號;playing 餵給
  // 頁尾共用的 VideoPlayerModal(同 /pikpak 頁的模式)。
  const [codeFiles, setCodeFiles] = useState<Map<string, PresenceCodeFiles>>(
    new Map()
  );
  const [codeFilesBusy, setCodeFilesBusy] = useState<Set<string>>(new Set());
  const [playing, setPlaying] = useState<{ id: string; name: string } | null>(
    null
  );

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
      setMissingError(null);
    } catch (e: any) {
      // Keep the previous badges — one failed refresh must not blank
      // the whole board. Only rows that never had data stay empty.
      setMissingError(e?.message || "缺漏摘要更新失敗");
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
        if (event.last_full_scan_at !== undefined)
          next.last_full_scan_at = event.last_full_scan_at;
        return next;
      }),
    );

    if (typeof event.missing_count === "number") {
      setMissing((prev) => {
        const next = new Map(prev ?? []);
        const existing = next.get(key);
        // Create the entry when absent (row added after the last summary
        // build, or the summary never loaded) instead of dropping the
        // fresh count on the floor — total stays 0 until the cheap
        // follow-up summary reload reconciles it.
        next.set(key, {
          kind: event.kind,
          id: event.id,
          name: event.name ?? "",
          total: 0,
          extras_count: 0,
          pages_scanned: 0,
          expected_root: "",
          error: "",
          catalog_fetched_at: null,
          ...(existing ?? {}),
          missing_count: event.missing_count,
        });
        return next;
      });
    }
  }, []);

  // Refresh just ONE row's 缺漏 badge from /missing-codes after a single
  // 立即檢查. We deliberately avoid re-fetching /missing-summary here (it
  // re-walks every tracked listing — minutes); this one listing's catalog
  // is already warm from the check we just ran, so it's cheap. Unlike the
  // done-event patch in patchRowFromEvent, this gives the badge an
  // accurate, deduped total / missing_count / extras_count — the patch
  // alone can't supply `total` (so a "未取得列表" row stays stuck) and
  // no-ops on rows that have no summary entry yet (e.g. just手動新增).
  const refreshRowMissing = useCallback(async (it: TrackedListing) => {
    const key = `${it.kind}:${it.id}`;
    try {
      const res = await api.get<MissingCodesResult>(
        `/api/tracked/${it.kind}/${encodeURIComponent(it.id)}/missing-codes`,
      );
      setMissing((prev) => {
        const next = new Map(prev ?? []);
        next.set(key, {
          kind: res.kind,
          id: res.id,
          name: res.name,
          total: res.total,
          missing_count: res.missing.length,
          extras_count: res.extras.length,
          pages_scanned: res.pages_scanned,
          expected_root: res.expected_root,
          error: "",
          catalog_fetched_at: res.catalog_fetched_at ?? null,
        });
        return next;
      });
      // Reuse the same payload for the (collapsed) detail panel so a later
      // expand renders instantly without another round-trip.
      setDetails((m) => new Map(m).set(key, res));
    } catch (e: any) {
      // Don't swallow: flag the row's badge as errored (red「缺漏 ?」) so a
      // JavBus 429/5xx during the post-check re-fetch doesn't leave a
      // stale/blank count looking authoritative, and tell the user.
      const msg = e?.message || "讀取缺漏失敗";
      setMissing((prev) => {
        if (!prev) return prev;
        const next = new Map(prev);
        const existing = next.get(key);
        if (existing) next.set(key, { ...existing, error: msg });
        return next;
      });
      toast.error(`${it.name || it.id} 缺漏重算失敗：${msg}`);
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

  async function checkNow(it: TrackedListing, deep = false) {
    const key = keyOf(it);
    setCheckingKey(key);
    setCheckingPhase("page 1…");
    setLastCheck(null);
    let finalResult: CheckListingResult | null = null;
    // Tracked separately from finalResult: TS can't see the callback's
    // assignment, so finalResult narrows to null in this outer scope and
    // reading finalResult.error here would be a `never` access.
    let checkErrored = false;
    try {
      // Stream the per-phase events so the button label can update from
      // "page 1…" → "掃描缺漏…" → "完成" instead of just showing a static
      // "檢查中" while the catalog walk runs in the background.
      await streamNdjson(
        `/api/tracked/${it.kind}/${encodeURIComponent(it.id)}/check/stream${
          deep ? "?deep=true" : ""
        }`,
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
            checkErrored = Boolean(errMsg);
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
      // Refresh just this row's 缺漏 badge (+ detail cache) from
      // /missing-codes so the count actually shows after a check. The
      // done-event patch can't supply `total` and no-ops on rows with no
      // summary entry, which left the badge blank / stuck. On error / no
      // result there's nothing fresh to show — drop any stale detail so a
      // later expand re-fetches instead.
      if (finalResult && !checkErrored) {
        await refreshRowMissing(it);
      } else {
        setDetails((m) => {
          const next = new Map(m);
          next.delete(key);
          return next;
        });
      }
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

  // 第一次點=讀 presence 索引;已查過再點=先請後端向 PikPak 重讀該番號
  // 的資料夾(更新索引),再取最新位置——改名/搬移後索引殘留舊 leaf 時用。
  async function lookupCode(code: string) {
    if (lookupBusy.has(code)) return;
    const live = lookups.has(code);
    setLookupBusy((s) => new Set(s).add(code));
    try {
      if (live) {
        await api.post(`/api/pikpak/presence/refresh-codes`, {
          codes: [code],
        });
      }
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

  // 查一個番號在 PikPak 上的影片檔。恰好一支直接開播;多支只快取結果,
  // 由 MissingDetailPanel 展開檔案列表讓使用者挑;已查過的再點視為重播。
  async function loadCodeFiles(code: string) {
    if (codeFilesBusy.has(code)) return;
    const cached = codeFiles.get(code);
    if (cached) {
      if (cached.files.length === 1) setPlaying(cached.files[0]);
      return;
    }
    setCodeFilesBusy((s) => new Set(s).add(code));
    try {
      const res = await api.get<PresenceCodeFiles>(
        `/api/pikpak/presence/codes/${encodeURIComponent(code)}/files`
      );
      setCodeFiles((m) => new Map(m).set(code, res));
      if (res.files.length === 1) {
        setPlaying(res.files[0]);
      } else if (res.files.length === 0) {
        toast.error("PikPak 上找不到影片檔");
      }
    } catch (e: any) {
      toast.error(e.message || "查詢影片失敗");
    } finally {
      setCodeFilesBusy((s) => {
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
    // Expanded detail panels are kept: wiping them here threw away every
    // cached /missing-codes payload the user had opened, for no gain —
    // stale panels reconcile on the next expand.
    loadMissing(false);
    load();
  }

  const filtered = filter ? items.filter((i) => i.kind === filter) : items;

  return (
    <div className="space-y-4">
      <TrackedToolbar
        filteredCount={filtered.length}
        totalCount={items.length}
        filter={filter}
        onFilterChange={setFilter}
        trackerStatus={trackerStatus}
        onToggleBackgroundScan={toggleBackgroundScan}
        missingLoading={missingLoading}
        anyChecking={!!checkingKey}
        batchActive={batchModalMode !== null}
        onOpenMissingSummary={openBatchMissingSummary}
        onOpenCheckAll={openBatchCheckAll}
      />

      {error && <ErrorBox message={error} />}

      {missingError && (
        <div className="rounded-md border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-sm text-amber-300">
          缺漏摘要更新失敗（顯示上次結果）：{missingError}
        </div>
      )}

      {trackerStatus?.scan_in_progress && (
        <TrackerStatusBar status={trackerStatus} />
      )}

      <AddTrackedForm onAdded={load} onError={setError} />

      {lastCheck && (
        <div
          className={
            "rounded-md border px-3 py-2 text-sm " +
            (lastCheck.error
              ? "border-red-500/30 bg-red-500/10 text-red-300"
              : lastCheck.new_codes.length
              ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
              : "border-border bg-muted/50 text-muted-foreground")
          }
        >
          {lastCheck.error
            ? `${TRACKED_LABELS[lastCheck.kind]} ${lastCheck.name || lastCheck.id}: ${lastCheck.error}`
            : lastCheck.new_codes.length
            ? `${TRACKED_LABELS[lastCheck.kind]} ${lastCheck.name} 有 ${lastCheck.new_codes.length} 部新作品: ${lastCheck.new_codes.join(", ")}`
            : `${TRACKED_LABELS[lastCheck.kind]} ${lastCheck.name} 沒有新作品`}
        </div>
      )}

      <TrackedList
        items={filtered}
        filter={filter}
        missing={missing}
        missingLoading={missingLoading}
        expanded={expanded}
        details={details}
        detailLoading={detailLoading}
        checkingKey={checkingKey}
        checkingPhase={checkingPhase}
        batchActive={batchModalMode !== null}
        lookups={lookups}
        lookupBusy={lookupBusy}
        codeFiles={codeFiles}
        codeFilesBusy={codeFilesBusy}
        onCheckNow={checkNow}
        onToggleExpand={toggleExpand}
        onLookup={lookupCode}
        onLoadFiles={loadCodeFiles}
        onPlay={setPlaying}
        onChanged={load}
      />

      <BatchScanModal
        open={batchModalMode !== null}
        mode={batchModalMode ?? "check-all"}
        onClose={() => setBatchModalMode(null)}
        onDone={onBatchModalDone}
        onProgress={patchRowFromEvent}
      />

      <VideoPlayerModal
        open={playing !== null}
        file={playing}
        onClose={() => setPlaying(null)}
      />
    </div>
  );
}
