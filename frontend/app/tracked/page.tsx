"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { confirmDialog, toast } from "@/components/Toast";
import {
  TRACKED_LABELS,
  api,
  imgProxy,
  type CheckListingResult,
  type MissingCodesResult,
  type MissingSummary,
  type MissingSummaryItem,
  type MovieListItem,
  type PresenceCodeLookup,
  type TrackedKind,
  type TrackedListing,
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
  const [lastCheck, setLastCheck] = useState<CheckListingResult | null>(null);
  const [filter, setFilter] = useState<TrackedKind | "">("");
  const [missing, setMissing] = useState<Map<string, MissingSummaryItem> | null>(
    null
  );
  const [missingLoading, setMissingLoading] = useState(false);
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

  useEffect(() => {
    loadMissing(false);
  }, [loadMissing]);

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
    setLastCheck(null);
    try {
      const res = await api.post<CheckListingResult>(
        `/api/tracked/${it.kind}/${encodeURIComponent(it.id)}/check`
      );
      setLastCheck(res);
      load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setCheckingKey(null);
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

  async function checkAll() {
    for (const it of filtered) {
      await checkNow(it);
    }
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
        {filtered.length > 0 && (
          <div className="ml-auto flex gap-2">
            <button
              className="btn-ghost"
              onClick={() => loadMissing(true)}
              disabled={missingLoading}
              title="重新掃 PikPak 資料夾並重算缺漏"
            >
              {missingLoading ? "重算缺漏…" : "重算缺漏"}
            </button>
            <button
              className="btn-ghost"
              onClick={checkAll}
              disabled={!!checkingKey}
            >
              {checkingKey ? "檢查中…" : "全部立即檢查"}
            </button>
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
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
                  if (m.missing_count > 0) {
                    return (
                      <button
                        onClick={() => toggleExpand(it)}
                        className="rounded bg-amber-400/20 px-2 py-0.5 text-xs text-amber-300 hover:bg-amber-400/30"
                        title={`全集 ${m.total} 部，掃 ${m.pages_scanned} 頁 · 點擊看明細`}
                      >
                        {m.missing_count} 個未下載{" "}
                        {expanded.has(keyOf(it)) ? "▾" : "▸"}
                      </button>
                    );
                  }
                  return (
                    <span
                      className="rounded bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-300"
                      title={`全集 ${m.total} 部都已下載`}
                    >
                      全收齊
                    </span>
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
              <label className="flex items-center gap-1">
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
                  disabled={checkingKey === keyOf(it)}
                  className="text-blue-300 hover:underline disabled:opacity-50"
                >
                  {checkingKey === keyOf(it) ? "檢查中" : "立即檢查"}
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
  if (!detail.missing.length) {
    return (
      <div className="border-t border-white/10 px-4 py-3 text-xs text-emerald-300/80">
        ✓ 已無缺漏
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
    </div>
  );
}
