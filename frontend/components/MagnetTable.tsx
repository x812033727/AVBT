"use client";

import { useMemo, useState } from "react";
import { api, type Magnet } from "@/lib/api";

type Status = { kind: "ok" | "err"; text: string } | null;

export default function MagnetTable({
  magnets,
  code,
}: {
  magnets: Magnet[];
  code: string;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Status>(null);

  const hdLinks = useMemo(
    () => magnets.filter((m) => m.is_hd).map((m) => m.link),
    [magnets]
  );

  function toggle(link: string) {
    const next = new Set(selected);
    if (next.has(link)) next.delete(link);
    else next.add(link);
    setSelected(next);
  }

  function selectAll() {
    if (selected.size === magnets.length) setSelected(new Set());
    else setSelected(new Set(magnets.map((m) => m.link)));
  }

  function selectHD() {
    setSelected(new Set(hdLinks));
  }

  async function sendOne(m: Magnet) {
    setBusy(true);
    setStatus(null);
    try {
      const task = await api.post<{ id: string; name: string; phase: string }>(
        "/api/pikpak/offline",
        { magnet: m.link, code }
      );
      setStatus({
        kind: "ok",
        text: `已送出：${task.name || task.id} (${task.phase || "pending"})`,
      });
    } catch (e: any) {
      setStatus({ kind: "err", text: `失敗：${e.message}` });
    } finally {
      setBusy(false);
    }
  }

  async function sendSelected() {
    if (!selected.size) return;
    setBusy(true);
    setStatus(null);
    try {
      const items = magnets
        .filter((m) => selected.has(m.link))
        .map((m) => ({ magnet: m.link, code }));
      const tasks = await api.post<
        { id: string; name: string; phase: string; message: string | null }[]
      >("/api/pikpak/offline/bulk", items);
      const ok = tasks.filter((t) => t.phase !== "ERROR").length;
      const fail = tasks.length - ok;
      setStatus({
        kind: fail ? "err" : "ok",
        text: `共 ${tasks.length} 個任務：成功 ${ok}，失敗 ${fail}`,
      });
      setSelected(new Set());
    } catch (e: any) {
      setStatus({ kind: "err", text: `批次失敗：${e.message}` });
    } finally {
      setBusy(false);
    }
  }

  if (!magnets.length) {
    return (
      <div className="rounded-lg border border-white/10 bg-panel p-6 text-center text-white/60">
        沒有抓到磁力連結（可能該番號目前無資源，或被反爬擋下）。
      </div>
    );
  }

  const allSelected = selected.size === magnets.length;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-ghost" onClick={selectAll}>
          {allSelected ? "全部取消" : "全選"}
        </button>
        {hdLinks.length > 0 && (
          <button className="btn-ghost" onClick={selectHD}>
            僅選高清 ({hdLinks.length})
          </button>
        )}
        <button
          className="btn-primary disabled:opacity-50"
          onClick={sendSelected}
          disabled={busy || !selected.size}
        >
          送 PikPak ({selected.size})
        </button>
      </div>

      {status && (
        <div
          className={
            "rounded-md border px-3 py-2 text-sm " +
            (status.kind === "ok"
              ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
              : "border-red-500/30 bg-red-500/10 text-red-300")
          }
        >
          {status.text}
        </div>
      )}

      <div className="overflow-hidden rounded-lg border border-white/10">
        <table className="w-full text-sm">
          <thead className="bg-white/5 text-left text-xs uppercase tracking-wide text-white/40">
            <tr>
              <th className="px-3 py-2 w-10"></th>
              <th className="px-3 py-2">名稱 / 標籤</th>
              <th className="px-3 py-2 w-24">大小</th>
              <th className="px-3 py-2 w-28">日期</th>
              <th className="px-3 py-2 w-32">動作</th>
            </tr>
          </thead>
          <tbody>
            {magnets.map((m) => (
              <tr key={m.link} className="border-t border-white/5">
                <td className="px-3 py-2">
                  <input
                    type="checkbox"
                    checked={selected.has(m.link)}
                    onChange={() => toggle(m.link)}
                    className="h-4 w-4 accent-accent"
                  />
                </td>
                <td className="px-3 py-2">
                  <div className="font-mono text-xs text-white/80 break-all">
                    {m.name || m.link}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {m.is_hd && <span className="tag-hd">高清</span>}
                    {m.has_subtitle && <span className="tag-sub">字幕</span>}
                  </div>
                </td>
                <td className="px-3 py-2 text-white/70">{m.size}</td>
                <td className="px-3 py-2 text-white/50">{m.date}</td>
                <td className="px-3 py-2">
                  <div className="flex gap-1">
                    <button
                      onClick={() => sendOne(m)}
                      disabled={busy}
                      className="btn-primary disabled:opacity-50"
                    >
                      送
                    </button>
                    <a
                      href={m.link}
                      className="btn-ghost"
                      title="用本地下載軟體開啟"
                    >
                      開啟
                    </a>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
