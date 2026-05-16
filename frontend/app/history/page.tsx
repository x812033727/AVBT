"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, btih, type HistoryPage } from "@/lib/api";
import { confirmDialog, toast } from "@/components/Toast";

const PAGE_SIZE = 50;

const ARCHIVE_OPTIONS = [
  { value: "", label: "全部" },
  { value: "false", label: "未歸檔" },
  { value: "true", label: "已歸檔" },
];

function fmt(d: string | null): string {
  if (!d) return "-";
  const date = new Date(d.endsWith("Z") ? d : d + "Z");
  return date.toLocaleString();
}

export default function HistoryListPage() {
  const [code, setCode] = useState("");
  const [debouncedCode, setDebouncedCode] = useState("");
  const [archived, setArchived] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<HistoryPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedCode(code.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [code]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (debouncedCode) params.set("code", debouncedCode);
      if (archived) params.set("archived", archived);
      const res = await api.get<HistoryPage>(
        `/api/collection/history?${params.toString()}`
      );
      setData(res);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [debouncedCode, archived, offset]);

  useEffect(() => {
    load();
  }, [load]);

  async function remove(id: number) {
    const ok = await confirmDialog("刪除此筆紀錄？", "不會刪 PikPak 上的檔案");
    if (!ok) return;
    try {
      await api.del(`/api/collection/history/${id}`);
      toast.success("已刪除紀錄");
      load();
    } catch (e: any) {
      toast.error(e.message);
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
            placeholder="篩選番號（精確匹配）"
            className="w-48 rounded-md border border-white/10 bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
          />
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
        {data && (
          <div className="ml-auto text-xs text-white/50">
            共 {data.total} 筆，第 {page} / {Math.max(totalPages, 1)} 頁
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
                <th className="px-3 py-2 w-32">送出時間</th>
                <th className="px-3 py-2 w-24">番號</th>
                <th className="px-3 py-2">名稱 / 磁力</th>
                <th className="px-3 py-2 w-28">狀態</th>
                <th className="px-3 py-2 w-32">歸檔</th>
                <th className="px-3 py-2 w-16">操作</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((it) => (
                <tr key={it.id} className="border-t border-white/5">
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
