"use client";

import { useMemo, useState } from "react";
import { api, btih, type Magnet } from "@/lib/api";

type Status = { kind: "ok" | "err"; text: string } | null;
type SortMode = "recommended" | "date" | "raw";

function sizeBytes(s: string): number {
  const m = s.match(/^([\d.]+)\s*([KMGT]?i?B)/i);
  if (!m) return 0;
  const n = parseFloat(m[1]);
  const unit = m[2].toUpperCase().replace("I", "");
  const mult: Record<string, number> = {
    B: 1,
    KB: 1024,
    MB: 1024 ** 2,
    GB: 1024 ** 3,
    TB: 1024 ** 4,
  };
  return n * (mult[unit] ?? 1);
}

export default function MagnetTable({
  magnets,
  code,
  sentHashes,
}: {
  magnets: Magnet[];
  code: string;
  sentHashes?: Set<string>;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Status>(null);
  const [sort, setSort] = useState<SortMode>("recommended");
  const [minMb, setMinMb] = useState("");
  const [maxMb, setMaxMb] = useState("");

  const sorted = useMemo(() => {
    const minB = (parseFloat(minMb) || 0) * 1024 * 1024;
    const maxB = (parseFloat(maxMb) || 0) * 1024 * 1024;
    let arr = magnets.filter((m) => {
      const b = sizeBytes(m.size);
      if (b <= 0) return true; // unknown size → keep
      if (minB && b < minB) return false;
      if (maxB && b > maxB) return false;
      return true;
    });
    if (sort === "raw") return arr;
    arr = [...arr];
    if (sort === "date") {
      arr.sort((a, b) => b.date.localeCompare(a.date));
      return arr;
    }
    // recommended: HD > 字幕 > size desc > date desc
    arr.sort((a, b) => {
      if (a.is_hd !== b.is_hd) return a.is_hd ? -1 : 1;
      if (a.has_subtitle !== b.has_subtitle) return a.has_subtitle ? -1 : 1;
      const sb = sizeBytes(b.size) - sizeBytes(a.size);
      if (sb !== 0) return sb;
      return b.date.localeCompare(a.date);
    });
    return arr;
  }, [magnets, sort, minMb, maxMb]);

  const hdLinks = useMemo(
    () => sorted.filter((m) => m.is_hd).map((m) => m.link),
    [sorted]
  );

  function toggle(link: string) {
    const next = new Set(selected);
    if (next.has(link)) next.delete(link);
    else next.add(link);
    setSelected(next);
  }

  function selectAll() {
    if (selected.size === sorted.length) setSelected(new Set());
    else setSelected(new Set(sorted.map((m) => m.link)));
  }

  function selectHD() {
    setSelected(new Set(hdLinks));
  }

  async function sendOne(m: Magnet, force = false) {
    setBusy(true);
    setStatus(null);
    try {
      const task = await api.post<{ id: string; name: string; phase: string }>(
        "/api/pikpak/offline",
        { magnet: m.link, code, force }
      );
      setStatus({
        kind: "ok",
        text: `已送出：${task.name || task.id} (${task.phase || "pending"})`,
      });
    } catch (e: any) {
      const msg = e.message || "";
      if (!force && msg.includes("已經送過")) {
        if (confirm(`${msg}\n\n要強制再送一次嗎？`)) {
          return sendOne(m, true);
        }
        setStatus({ kind: "err", text: msg });
      } else {
        setStatus({ kind: "err", text: `失敗：${msg}` });
      }
    } finally {
      setBusy(false);
    }
  }

  async function sendSelected(force = false) {
    if (!selected.size) return;
    setBusy(true);
    setStatus(null);
    try {
      const items = sorted
        .filter((m) => selected.has(m.link))
        .map((m) => ({ magnet: m.link, code, force }));
      const tasks = await api.post<
        { id: string; name: string; phase: string; message: string | null }[]
      >("/api/pikpak/offline/bulk", items);
      const ok = tasks.filter(
        (t) => t.phase !== "ERROR" && t.phase !== "DUPLICATE"
      ).length;
      const dup = tasks.filter((t) => t.phase === "DUPLICATE").length;
      const fail = tasks.filter((t) => t.phase === "ERROR").length;
      const parts = [`成功 ${ok}`];
      if (dup) parts.push(`已送過 ${dup}`);
      if (fail) parts.push(`失敗 ${fail}`);
      setStatus({
        kind: fail ? "err" : "ok",
        text: `共 ${tasks.length} 個：${parts.join("，")}`,
      });
      if (dup && !force) {
        if (
          confirm(`有 ${dup} 個磁力已送過。要強制再送一次嗎？`)
        ) {
          // Re-send only the duplicates with force=true
          return sendSelected(true);
        }
      }
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

  const allSelected = selected.size === sorted.length;

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
          onClick={() => sendSelected(false)}
          disabled={busy || !selected.size}
        >
          送 PikPak ({selected.size})
        </button>
        <div className="flex items-center gap-1 text-xs text-white/60">
          <span>大小</span>
          <input
            type="number"
            min={0}
            placeholder="min"
            value={minMb}
            onChange={(e) => setMinMb(e.target.value)}
            className="w-16 rounded border border-white/10 bg-ink px-2 py-1 text-right outline-none focus:border-accent"
          />
          <span>~</span>
          <input
            type="number"
            min={0}
            placeholder="max"
            value={maxMb}
            onChange={(e) => setMaxMb(e.target.value)}
            className="w-16 rounded border border-white/10 bg-ink px-2 py-1 text-right outline-none focus:border-accent"
          />
          <span>MB</span>
        </div>
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as SortMode)}
          className="ml-auto rounded-md border border-white/10 bg-panel px-2 py-1 text-sm text-white/80"
          title="排序"
        >
          <option value="recommended">推薦排序</option>
          <option value="date">依日期</option>
          <option value="raw">原始順序</option>
        </select>
      </div>
      <div className="text-xs text-white/40">
        顯示 {sorted.length} / {magnets.length} 筆
        {(minMb || maxMb) && (
          <>
            {" "}（過濾：{minMb || "0"} ~ {maxMb || "∞"} MB；
            未標示大小的不會被過濾）
          </>
        )}
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
            {sorted.map((m) => {
              const sent = sentHashes?.has(btih(m.link)) ?? false;
              return (
              <tr
                key={m.link}
                className={
                  "border-t border-white/5 " + (sent ? "bg-emerald-400/5" : "")
                }
              >
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
                    {m.part_hint && (
                      <span
                        className="rounded bg-amber-400/20 px-2 py-0.5 text-xs text-amber-200"
                        title={`名稱含分集標記:${m.part_hint}(僅供參考,實際檔案數以下載後為準)`}
                      >
                        可能分集
                      </span>
                    )}
                    {sent && (
                      <span className="rounded bg-emerald-400/20 px-2 py-0.5 text-xs text-emerald-200">
                        已送過
                      </span>
                    )}
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
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
