"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  btih,
  type HistoryItem,
  type HistoryPage,
  type VideoCountResponse,
  type VideoCountResult,
} from "@/lib/api";
import { confirmDialog, toast } from "@/components/Toast";

const PAGE_SIZE = 50;

const ARCHIVE_OPTIONS = [
  { value: "", label: "全部" },
  { value: "false", label: "未歸檔" },
  { value: "true", label: "已歸檔" },
];

const PHASE_OPTIONS = [
  { value: "", label: "全部" },
  { value: "PHASE_TYPE_COMPLETE", label: "COMPLETE" },
  { value: "PHASE_TYPE_RUNNING", label: "RUNNING" },
  { value: "PHASE_TYPE_PENDING", label: "PENDING" },
  { value: "PHASE_TYPE_ERROR", label: "ERROR" },
];

function fmt(d: string | null): string {
  if (!d) return "-";
  const date = new Date(d.endsWith("Z") ? d : d + "Z");
  return date.toLocaleString();
}

export default function HistoryListPage() {
  const [code, setCode] = useState("");
  const [debouncedCode, setDebouncedCode] = useState("");
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [archived, setArchived] = useState("");
  const [phase, setPhase] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<HistoryPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [counts, setCounts] = useState<Record<number, VideoCountResult | "loading">>({});
  const [counting, setCounting] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedCode(code.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [code]);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedQ(q.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [q]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (debouncedCode) params.set("code", debouncedCode);
      if (debouncedQ) params.set("q", debouncedQ);
      if (archived) params.set("archived", archived);
      if (phase) params.set("phase", phase);
      const res = await api.get<HistoryPage>(
        `/api/collection/history?${params.toString()}`
      );
      setData(res);
      // Rows may have vanished under the selection between loads.
      setSelected((prev) => {
        const alive = new Set(res.items.map((i) => i.id));
        const next = new Set<number>();
        for (const id of Array.from(prev)) if (alive.has(id)) next.add(id);
        return next;
      });
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [debouncedCode, debouncedQ, archived, phase, offset]);

  useEffect(() => {
    load();
  }, [load]);

  async function remove(id: number) {
    const ok = await confirmDialog("刪除此筆紀錄?", "不會刪 PikPak 上的檔案");
    if (!ok) return;
    try {
      await api.del(`/api/collection/history/${id}`);
      toast.success("已刪除紀錄");
      load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // 影片數查詢:已歸檔列用番號解析(歸檔時 wrapper 會被扁平化,
  // file_id 已失效);未歸檔列直接查任務的 file_id。
  function countEligible(it: HistoryItem): boolean {
    return (it.archived && !!it.code) || !!it.file_id;
  }

  function countItemFor(it: HistoryItem) {
    if (it.archived && it.code) return { key: String(it.id), code: it.code };
    return { key: String(it.id), file_id: it.file_id };
  }

  async function fetchCounts(rows: HistoryItem[]) {
    const targets = rows.filter((it) => countEligible(it) && !counts[it.id]);
    if (!targets.length) return;
    setCounting(true);
    setCounts((prev) => {
      const next = { ...prev };
      for (const it of targets) next[it.id] = "loading";
      return next;
    });
    try {
      for (let i = 0; i < targets.length; i += 20) {
        const chunk = targets.slice(i, i + 20);
        const res = await api.post<VideoCountResponse>(
          "/api/pikpak/files/video-count",
          { items: chunk.map(countItemFor) }
        );
        setCounts((prev) => {
          const next = { ...prev };
          for (const r of res.results) next[Number(r.key)] = r;
          return next;
        });
      }
    } catch (e: any) {
      toast.error(`影片數查詢失敗:${e.message}`);
      setCounts((prev) => {
        const next = { ...prev };
        for (const it of targets) {
          if (next[it.id] === "loading") delete next[it.id];
        }
        return next;
      });
    } finally {
      setCounting(false);
    }
  }

  // 已歸檔列自動查詢:走番號 → presence 索引,不打任務 API,成本低。
  // 未歸檔列(要打 PikPak 任務查詢)維持手動「查」。
  useEffect(() => {
    if (!data) return;
    const archivedRows = data.items.filter((it) => it.archived && it.code);
    if (archivedRows.length) fetchCounts(archivedRows);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  function toggleRow(id: number, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  const allOnPageSelected = useMemo(
    () => !!data?.items.length && data.items.every((i) => selected.has(i.id)),
    [data, selected]
  );

  function toggleAll(on: boolean) {
    if (!data) return;
    setSelected((prev) => {
      const next = new Set(prev);
      for (const it of data.items) {
        if (on) next.add(it.id);
        else next.delete(it.id);
      }
      return next;
    });
  }

  async function batchDelete() {
    const ids = Array.from(selected);
    if (!ids.length) return;
    const ok = await confirmDialog(
      `刪除選取的 ${ids.length} 筆紀錄?`,
      "不會刪 PikPak 上的檔案"
    );
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api.post<{ deleted: number }>(
        "/api/collection/history/batch-delete",
        { ids }
      );
      toast.success(`已刪除 ${r.deleted} 筆紀錄`);
      setSelected(new Set());
      load();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function batchRearchive() {
    const ids = Array.from(selected);
    if (!ids.length) return;
    const ok = await confirmDialog(
      `把選取的 ${ids.length} 筆標回「未歸檔」?`,
      "歸檔器下一輪會重新解析並搬移這些檔案"
    );
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api.post<{ updated: number }>(
        "/api/collection/history/batch-rearchive",
        { ids }
      );
      toast.success(`已標記 ${r.updated} 筆待重新歸檔`);
      setSelected(new Set());
      load();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function batchResend() {
    if (!data) return;
    const rows = data.items.filter((i) => selected.has(i.id) && i.magnet);
    if (!rows.length) return;
    const ok = await confirmDialog(
      `重新送出選取的 ${rows.length} 個磁力連結到 PikPak?`,
      "使用原本的磁力連結,強制送出(略過已送過檢查)"
    );
    if (!ok) return;
    setBusy(true);
    try {
      const results = await api.post<{ phase: string }[]>(
        "/api/pikpak/offline/bulk",
        rows.map((r) => ({ magnet: r.magnet, code: r.code, force: true }))
      );
      const okCount = results.filter(
        (t) => t.phase !== "ERROR" && t.phase !== "DUPLICATE"
      ).length;
      toast.success(`已重新送出 ${okCount} / ${rows.length} 個`);
      setSelected(new Set());
      load();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 1;

  return (
    <div className="space-y-4">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setOffset(0);
          load();
        }}
      >
        <div>
          <div className="text-xs text-white/40">番號</div>
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="篩選番號(精確匹配)"
            className="w-40 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
          />
        </div>
        <div>
          <div className="text-xs text-white/40">名稱</div>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜尋檔案名稱"
            className="w-48 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
          />
        </div>
        <div>
          <div className="text-xs text-white/40">狀態</div>
          <select
            value={phase}
            onChange={(e) => {
              setPhase(e.target.value);
              setOffset(0);
            }}
            className="rounded-md border border-white/10 bg-panel px-2 py-1 text-sm"
          >
            {PHASE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <div className="text-xs text-white/40">歸檔狀態</div>
          <select
            value={archived}
            onChange={(e) => {
              setArchived(e.target.value);
              setOffset(0);
            }}
            className="rounded-md border border-white/10 bg-panel px-2 py-1 text-sm"
          >
            {ARCHIVE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <button type="submit" className="btn-ghost" disabled={loading}>
          {loading ? "讀取中…" : "刷新"}
        </button>
        <button
          type="button"
          className="btn-ghost"
          disabled={counting || !data?.items.some((it) => countEligible(it) && !counts[it.id])}
          onClick={() => data && fetchCounts(data.items)}
          title="向 PikPak 查詢本頁每筆任務實際的影片檔數(分集/單一)"
        >
          {counting ? "查詢中…" : "查詢本頁影片數"}
        </button>
        {data && (
          <div className="ml-auto text-xs text-white/50">
            共 {data.total} 筆,第 {page} / {Math.max(totalPages, 1)} 頁
          </div>
        )}
      </form>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {data && !data.items.length && (
        <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
          沒有紀錄
        </div>
      )}

      {data && !!data.items.length && (
        <div className="overflow-hidden rounded-lg border border-white/10">
          <table className="w-full text-sm">
            <thead className="bg-white/5 text-left text-xs uppercase tracking-wide text-white/40">
              <tr>
                <th className="w-8 px-3 py-2">
                  <input
                    type="checkbox"
                    checked={allOnPageSelected}
                    onChange={(e) => toggleAll(e.target.checked)}
                    title="全選本頁"
                  />
                </th>
                <th className="w-32 px-3 py-2">送出時間</th>
                <th className="w-24 px-3 py-2">番號</th>
                <th className="px-3 py-2">名稱 / 磁力</th>
                <th className="w-28 px-3 py-2">狀態</th>
                <th className="w-32 px-3 py-2">歸檔</th>
                <th className="w-24 px-3 py-2">影片數</th>
                <th className="w-16 px-3 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((it) => (
                <tr key={it.id} className="border-t border-white/5">
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={selected.has(it.id)}
                      onChange={(e) => toggleRow(it.id, e.target.checked)}
                    />
                  </td>
                  <td className="px-3 py-2 text-white/60">
                    {fmt(it.created_at)}
                  </td>
                  <td className="px-3 py-2">
                    {it.code ? (
                      <Link
                        href={`/movie/${encodeURIComponent(it.code)}`}
                        className="font-mono text-accent hover:underline"
                      >
                        {it.code}
                      </Link>
                    ) : (
                      <span className="text-white/30">-</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <div className="truncate text-white/80">
                      {it.name || "(未命名)"}
                    </div>
                    <div className="truncate font-mono text-xs text-white/30">
                      {btih(it.magnet)}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={
                        "rounded px-2 py-0.5 text-xs " +
                        (it.phase === "PHASE_TYPE_COMPLETE"
                          ? "bg-emerald-400/20 text-emerald-200"
                          : it.phase === "PHASE_TYPE_ERROR"
                          ? "bg-red-500/20 text-red-300"
                          : "bg-white/10 text-white/70")
                      }
                    >
                      {it.phase.replace("PHASE_TYPE_", "") || "—"}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-xs">
                    {it.archived ? (
                      <span className="text-emerald-300">
                        ✓ {fmt(it.archived_at)}
                      </span>
                    ) : (
                      <span className="text-white/30">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs">
                    <VideoCountCell
                      state={counts[it.id]}
                      eligible={countEligible(it)}
                      onQuery={() => fetchCounts([it])}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => remove(it.id)}
                      className="text-red-300 hover:underline"
                    >
                      刪除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected.size > 0 && (
        <div className="sticky bottom-3 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-white/10 bg-panel/95 px-4 py-3 shadow-lg backdrop-blur">
          <span className="text-sm text-white/70">已選 {selected.size} 筆</span>
          <button
            className="rounded-md border border-white/10 px-3 py-1.5 text-sm text-white/80 transition hover:bg-white/5 disabled:opacity-50"
            onClick={batchResend}
            disabled={busy}
          >
            重送磁力
          </button>
          <button
            className="rounded-md border border-white/10 px-3 py-1.5 text-sm text-white/80 transition hover:bg-white/5 disabled:opacity-50"
            onClick={batchRearchive}
            disabled={busy}
          >
            重新歸檔
          </button>
          <button
            className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-sm text-red-300 transition hover:bg-red-500/20 disabled:opacity-50"
            onClick={batchDelete}
            disabled={busy}
          >
            刪除紀錄
          </button>
          <button
            className="btn-ghost text-sm"
            onClick={() => setSelected(new Set())}
            disabled={busy}
          >
            清除選取
          </button>
        </div>
      )}

      <div className="flex justify-center gap-2">
        <button
          className="btn-ghost"
          disabled={loading || offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          上一頁
        </button>
        <button
          className="btn-ghost"
          disabled={
            loading || !data || offset + PAGE_SIZE >= (data?.total ?? 0)
          }
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          下一頁
        </button>
      </div>
    </div>
  );
}

function VideoCountCell({
  state,
  eligible,
  onQuery,
}: {
  state: VideoCountResult | "loading" | undefined;
  eligible: boolean;
  onQuery: () => void;
}) {
  if (!eligible) return <span className="text-white/30">—</span>;
  if (state === undefined) {
    return (
      <button
        onClick={onQuery}
        className="rounded border border-white/10 px-2 py-0.5 text-white/50 hover:bg-white/10"
        title="向 PikPak 查詢實際影片檔數"
      >
        查
      </button>
    );
  }
  if (state === "loading") return <span className="text-white/40">…</span>;
  if (!state.ok) {
    return (
      <span className="text-white/30" title={state.error}>
        —
      </span>
    );
  }
  const tip =
    state.video_names.join("\n") ||
    state.entries.map((e) => `${e.path}(${e.video_count})`).join("\n") ||
    undefined;
  if (state.video_count > 1) {
    return (
      <span
        className="rounded bg-amber-400/20 px-2 py-0.5 text-amber-200"
        title={tip}
      >
        多集 {state.video_count}
      </span>
    );
  }
  if (state.video_count === 1) {
    return (
      <span className="text-white/60" title={tip}>
        單一
      </span>
    );
  }
  return (
    <span className="text-white/40" title="任務裡目前沒有影片檔(可能還在下載)">
      0
    </span>
  );
}
