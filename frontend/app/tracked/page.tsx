"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  TRACKED_LABELS,
  api,
  type CheckListingResult,
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

  function keyOf(it: TrackedListing) {
    return `${it.kind}:${it.id}`;
  }

  async function remove(it: TrackedListing) {
    if (!confirm(`不再追蹤 ${TRACKED_LABELS[it.kind]} ${it.name || it.id}？`)) return;
    await api.del(`/api/tracked/${it.kind}/${encodeURIComponent(it.id)}`);
    load();
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

  async function checkAll() {
    for (const it of filtered) {
      await checkNow(it);
    }
  }

  async function manualAdd(e: React.FormEvent) {
    e.preventDefault();
    const slug = addSlug.trim();
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
          <button
            className="ml-auto btn-ghost"
            onClick={checkAll}
            disabled={!!checkingKey}
          >
            {checkingKey ? "檢查中…" : "全部立即檢查"}
          </button>
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
            className="flex flex-wrap gap-3 rounded-lg border border-white/10 bg-panel p-3"
          >
            {it.avatar ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={it.avatar}
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
        ))}
      </div>
    </div>
  );
}
