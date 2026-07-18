"use client";

import Link from "next/link";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Check, History, Loader2 } from "lucide-react";
import {
  api,
  btih,
  type HistoryItem,
  type HistoryPage,
  type VideoCountResponse,
  type VideoCountResult,
} from "@/lib/api";
import { confirmDialog, toast } from "@/components/Toast";
import { fmtDateTime } from "@/lib/format";
import { pikpakPhaseTone } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBox } from "@/components/shared/ErrorBox";
import { StatusBadge } from "@/components/shared/StatusBadge";

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

const ABANDONED_OPTIONS = [
  { value: "", label: "全部" },
  { value: "true", label: "只看已放棄" },
  { value: "false", label: "排除已放棄" },
];

const SELECT_CLASS =
  "h-9 rounded-md border border-input bg-background px-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

export default function HistoryListPage() {
  return (
    <Suspense fallback={<div className="text-sm text-muted-foreground">載入中…</div>}>
      <HistoryListPageInner />
    </Suspense>
  );
}

function HistoryListPageInner() {
  const searchParams = useSearchParams();
  const [code, setCode] = useState("");
  const [debouncedCode, setDebouncedCode] = useState("");
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [archived, setArchived] = useState("");
  const [abandoned, setAbandoned] = useState(() => {
    const v = searchParams.get("abandoned");
    return v === "true" || v === "false" ? v : "";
  });
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
      if (abandoned) params.set("abandoned", abandoned);
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
  }, [debouncedCode, debouncedQ, archived, abandoned, phase, offset]);

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
          <div className="mb-1 text-xs text-muted-foreground">番號</div>
          <Input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="篩選番號(精確匹配)"
            className="w-40"
          />
        </div>
        <div>
          <div className="mb-1 text-xs text-muted-foreground">名稱</div>
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜尋檔案名稱"
            className="w-48"
          />
        </div>
        <div>
          <div className="mb-1 text-xs text-muted-foreground">狀態</div>
          <select
            value={phase}
            onChange={(e) => {
              setPhase(e.target.value);
              setOffset(0);
            }}
            className={SELECT_CLASS}
          >
            {PHASE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <div className="mb-1 text-xs text-muted-foreground">歸檔狀態</div>
          <select
            value={archived}
            onChange={(e) => {
              setArchived(e.target.value);
              setOffset(0);
            }}
            className={SELECT_CLASS}
          >
            {ARCHIVE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <div className="mb-1 text-xs text-muted-foreground">死信</div>
          <select
            value={abandoned}
            onChange={(e) => {
              setAbandoned(e.target.value);
              setOffset(0);
            }}
            className={SELECT_CLASS}
          >
            {ABANDONED_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" variant="outline" disabled={loading}>
          {loading ? "讀取中…" : "刷新"}
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={counting || !data?.items.some((it) => countEligible(it) && !counts[it.id])}
          onClick={() => data && fetchCounts(data.items)}
          title="向 PikPak 查詢本頁每筆任務實際的影片檔數(分集/單一)"
        >
          {counting ? "查詢中…" : "查詢本頁影片數"}
        </Button>
        {data && (
          <div className="ml-auto text-xs text-muted-foreground">
            共 {data.total} 筆,第 {page} / {Math.max(totalPages, 1)} 頁
          </div>
        )}
      </form>

      {error && <ErrorBox message={error} onRetry={load} />}

      {data && !data.items.length && (
        <EmptyState icon={History} title="沒有紀錄" />
      )}

      {data && !!data.items.length && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <Table>
            <TableHeader className="bg-muted/40 text-xs uppercase tracking-wide">
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-8 px-3">
                  <Checkbox
                    checked={allOnPageSelected}
                    onCheckedChange={(v) => toggleAll(v === true)}
                    title="全選本頁"
                    aria-label="全選本頁"
                  />
                </TableHead>
                <TableHead className="w-32 px-3">送出時間</TableHead>
                <TableHead className="w-24 px-3">番號</TableHead>
                <TableHead className="px-3">名稱 / 磁力</TableHead>
                <TableHead className="w-28 px-3">狀態</TableHead>
                <TableHead className="w-32 px-3">歸檔</TableHead>
                <TableHead className="w-24 px-3">影片數</TableHead>
                <TableHead className="w-16 px-3">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((it) => {
                const phaseView = pikpakPhaseTone(it.phase);
                return (
                  <TableRow key={it.id}>
                    <TableCell className="px-3">
                      <Checkbox
                        checked={selected.has(it.id)}
                        onCheckedChange={(v) => toggleRow(it.id, v === true)}
                        aria-label="選取此列"
                      />
                    </TableCell>
                    <TableCell className="px-3 text-muted-foreground">
                      {fmtDateTime(it.created_at)}
                    </TableCell>
                    <TableCell className="px-3">
                      {it.code ? (
                        <Link
                          href={`/movie/${encodeURIComponent(it.code)}`}
                          className="font-mono text-primary hover:underline"
                        >
                          {it.code}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground/50">-</span>
                      )}
                    </TableCell>
                    <TableCell className="px-3">
                      <div className="truncate text-foreground/80">
                        {it.name || "(未命名)"}
                      </div>
                      <div className="truncate font-mono text-xs text-muted-foreground/60">
                        {btih(it.magnet)}
                      </div>
                    </TableCell>
                    <TableCell className="px-3">
                      <div className="flex flex-wrap items-center gap-1">
                        <StatusBadge tone={phaseView.tone}>{phaseView.label}</StatusBadge>
                        {it.abandoned && (
                          <span title={it.message}>
                            <StatusBadge tone="warning">已放棄</StatusBadge>
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="px-3 text-xs">
                      {it.archived ? (
                        <span className="inline-flex items-center gap-1 text-emerald-300">
                          <Check className="h-3 w-3 shrink-0" aria-hidden />
                          {fmtDateTime(it.archived_at)}
                        </span>
                      ) : (
                        <span className="text-muted-foreground/50">—</span>
                      )}
                    </TableCell>
                    <TableCell className="px-3 text-xs">
                      <VideoCountCell
                        state={counts[it.id]}
                        eligible={countEligible(it)}
                        onQuery={() => fetchCounts([it])}
                      />
                    </TableCell>
                    <TableCell className="px-3">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => remove(it.id)}
                        className="h-7 px-2 text-red-300 hover:bg-red-500/10 hover:text-red-200"
                      >
                        刪除
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {selected.size > 0 && (
        <div className="sticky bottom-3 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card/95 px-4 py-3 shadow-lg backdrop-blur">
          <span className="text-sm text-foreground/70">已選 {selected.size} 筆</span>
          <Button variant="outline" size="sm" onClick={batchResend} disabled={busy}>
            重送磁力
          </Button>
          <Button variant="outline" size="sm" onClick={batchRearchive} disabled={busy}>
            重新歸檔
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="border-red-500/40 bg-red-500/10 text-red-300 hover:bg-red-500/20 hover:text-red-200"
            onClick={batchDelete}
            disabled={busy}
          >
            刪除紀錄
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

      <div className="flex justify-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={loading || offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          上一頁
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={
            loading || !data || offset + PAGE_SIZE >= (data?.total ?? 0)
          }
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          下一頁
        </Button>
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
  if (!eligible) return <span className="text-muted-foreground/50">—</span>;
  if (state === undefined) {
    return (
      <Button
        variant="outline"
        size="sm"
        onClick={onQuery}
        className="h-6 px-2 text-xs text-muted-foreground"
        title="向 PikPak 查詢實際影片檔數"
      >
        查
      </Button>
    );
  }
  if (state === "loading") {
    return (
      <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" aria-hidden />
    );
  }
  if (!state.ok) {
    return (
      <span className="text-muted-foreground/50" title={state.error}>
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
      <span title={tip}>
        <StatusBadge tone="warning">多集 {state.video_count}</StatusBadge>
      </span>
    );
  }
  if (state.video_count === 1) {
    return (
      <span className="text-muted-foreground" title={tip}>
        單一
      </span>
    );
  }
  return (
    <span
      className="text-muted-foreground/70"
      title="任務裡目前沒有影片檔(可能還在下載)"
    >
      0
    </span>
  );
}
