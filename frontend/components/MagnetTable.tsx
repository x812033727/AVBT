"use client";

import { useState } from "react";
import { api, type Magnet } from "@/lib/api";

export default function MagnetTable({
  magnets,
  code,
}: {
  magnets: Magnet[];
  code: string;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  async function sendToPikpak(m: Magnet) {
    setBusy(m.link);
    setMsg(null);
    try {
      const task = await api.post<{ id: string; name: string; phase: string }>(
        "/api/pikpak/offline",
        { magnet: m.link, code }
      );
      setMsg(`已送出：${task.name || task.id} (${task.phase || "pending"})`);
    } catch (e: any) {
      setMsg(`失敗：${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  if (!magnets.length) {
    return (
      <div className="rounded-lg border border-white/10 bg-panel p-6 text-center text-white/60">
        沒有抓到磁力連結（可能該番號目前無資源，或被反爬擋下）。
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {msg && (
        <div className="rounded-md border border-white/10 bg-white/5 px-3 py-2 text-sm text-white/80">
          {msg}
        </div>
      )}
      <div className="overflow-hidden rounded-lg border border-white/10">
        <table className="w-full text-sm">
          <thead className="bg-white/5 text-left text-xs uppercase tracking-wide text-white/40">
            <tr>
              <th className="px-3 py-2">名稱 / 標籤</th>
              <th className="px-3 py-2 w-24">大小</th>
              <th className="px-3 py-2 w-28">日期</th>
              <th className="px-3 py-2 w-44">動作</th>
            </tr>
          </thead>
          <tbody>
            {magnets.map((m) => (
              <tr key={m.link} className="border-t border-white/5">
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
                      onClick={() => sendToPikpak(m)}
                      disabled={busy === m.link}
                      className="btn-primary disabled:opacity-50"
                    >
                      {busy === m.link ? "送出中…" : "送 PikPak"}
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
