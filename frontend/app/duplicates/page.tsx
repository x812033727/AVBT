"use client";

import { useEffect, useState } from "react";
import CloudFolderPicker, {
  type CloudFolderSelection,
} from "@/components/CloudFolderPicker";
import { toast } from "@/components/Toast";
import {
  api,
  streamNdjson,
  type PCloudStatus,
  type PikPakStatus,
} from "@/lib/api";

type DupRow = {
  code: string;
  pikpak_paths: string[];
  pcloud_paths: string[];
};

type DupResult = {
  duplicates: DupRow[];
  duplicate_count: number;
  pikpak_codes: number;
  pcloud_codes: number;
  pikpak_items: number;
  pcloud_items: number;
  pikpak_partial: boolean;
  pcloud_partial: boolean;
};

export default function DuplicatesPage() {
  const [pikpakSel, setPikpakSel] = useState<CloudFolderSelection | null>(null);
  const [pcloudSel, setPcloudSel] = useState<CloudFolderSelection | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ pikpak?: string; pcloud?: string }>(
    {}
  );
  const [result, setResult] = useState<DupResult | null>(null);

  const [pikpakIn, setPikpakIn] = useState<boolean | null>(null);
  const [pcloudIn, setPcloudIn] = useState<boolean | null>(null);

  useEffect(() => {
    api
      .get<PikPakStatus>("/api/pikpak/status")
      .then((s) => setPikpakIn(!!s.logged_in))
      .catch(() => setPikpakIn(false));
    api
      .get<PCloudStatus>("/api/pcloud/status")
      .then((s) => setPcloudIn(!!s.logged_in))
      .catch(() => setPcloudIn(false));
  }, []);

  const bothReady = pikpakIn !== false && pcloudIn !== false;

  async function run() {
    if (running) return;
    setRunning(true);
    setResult(null);
    setProgress({});
    try {
      await streamNdjson(
        "/api/compare/duplicates/stream",
        {
          pikpak_folder_id: pikpakSel?.id ?? "",
          pcloud_folder_id: pcloudSel?.id ?? "0",
        },
        (ev: any) => {
          if (ev.type === "progress") {
            const label = `已掃描 ${ev.folders_done} 個資料夾・${ev.items_seen} 個項目・${ev.codes} 個番號`;
            setProgress((p) => ({ ...p, [ev.side]: label }));
          } else if (ev.type === "done") {
            setResult(ev.result as DupResult);
          } else if (ev.type === "error") {
            toast.error(ev.message || "比對失敗");
          }
        }
      );
    } catch (e: any) {
      toast.error(e.message || "比對失敗");
    } finally {
      setRunning(false);
    }
  }

  function copyCodes() {
    if (!result?.duplicates.length) return;
    const text = result.duplicates.map((d) => d.code).join("\n");
    navigator.clipboard
      .writeText(text)
      .then(() => toast.success(`已複製 ${result.duplicates.length} 個番號`))
      .catch(() => toast.error("複製失敗"));
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-white">跨雲重複番號比對</h1>
        <p className="mt-1 text-sm text-white/50">
          各選一個資料夾，遞迴掃描整個子樹，列出在 PikPak 與 pCloud
          兩邊都存在的番號。唯讀，不會搬移或刪除任何檔案。
        </p>
      </div>

      {(pikpakIn === false || pcloudIn === false) && (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
          {pikpakIn === false && pcloudIn === false
            ? "PikPak 與 pCloud 都尚未登入"
            : pikpakIn === false
            ? "PikPak 尚未登入"
            : "pCloud 尚未登入"}
          — 請先到對應頁面登入後再比對。
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <div className="text-sm font-medium text-white/80">PikPak 路徑</div>
          <CloudFolderPicker provider="pikpak" onChange={setPikpakSel} />
        </div>
        <div className="space-y-2">
          <div className="text-sm font-medium text-white/80">pCloud 路徑</div>
          <CloudFolderPicker provider="pcloud" onChange={setPcloudSel} />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          className="btn-primary"
          onClick={run}
          disabled={running || !bothReady}
        >
          {running ? "比對中…" : "開始比對"}
        </button>
        {(progress.pikpak || progress.pcloud) && (
          <div className="text-xs text-white/50">
            {progress.pikpak && <div>PikPak：{progress.pikpak}</div>}
            {progress.pcloud && <div>pCloud：{progress.pcloud}</div>}
          </div>
        )}
      </div>

      {result && <ResultPanel result={result} onCopy={copyCodes} />}
    </div>
  );
}

function ResultPanel({
  result,
  onCopy,
}: {
  result: DupResult;
  onCopy: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-white/5 bg-white/[0.03] px-3 py-2 text-sm text-white/70">
        <span>
          <span className="text-white/40">PikPak 番號</span> {result.pikpak_codes}
          <span className="ml-1 text-white/30">
            ({result.pikpak_items} 項)
          </span>
        </span>
        <span>
          <span className="text-white/40">pCloud 番號</span> {result.pcloud_codes}
          <span className="ml-1 text-white/30">
            ({result.pcloud_items} 項)
          </span>
        </span>
        <span className="font-medium text-accent">
          重複 {result.duplicate_count}
        </span>
        {(result.pikpak_partial || result.pcloud_partial) && (
          <span className="text-amber-300/80">(已達掃描上限，結果為部分)</span>
        )}
        {result.duplicate_count > 0 && (
          <button
            onClick={onCopy}
            className="ml-auto rounded border border-white/10 px-2 py-0.5 text-xs text-white/70 hover:bg-white/10"
          >
            複製番號清單
          </button>
        )}
      </div>

      {result.duplicate_count === 0 ? (
        <div className="rounded-md border border-white/10 bg-panel px-3 py-8 text-center text-white/50">
          沒有發現跨雲重複的番號
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-white/10">
          <table className="w-full text-sm">
            <thead className="bg-white/5 text-left text-xs uppercase tracking-wide text-white/40">
              <tr>
                <th className="px-3 py-2 w-40">番號</th>
                <th className="px-3 py-2">PikPak 路徑</th>
                <th className="px-3 py-2">pCloud 路徑</th>
              </tr>
            </thead>
            <tbody>
              {result.duplicates.map((d) => (
                <tr key={d.code} className="border-t border-white/5 align-top">
                  <td className="px-3 py-2 font-mono font-medium text-white/90">
                    {d.code}
                  </td>
                  <td className="px-3 py-2">
                    <PathList paths={d.pikpak_paths} />
                  </td>
                  <td className="px-3 py-2">
                    <PathList paths={d.pcloud_paths} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PathList({ paths }: { paths: string[] }) {
  if (!paths.length) return <span className="text-white/30">—</span>;
  return (
    <ul className="space-y-0.5">
      {paths.map((p, i) => (
        <li key={i} className="break-all font-mono text-xs text-white/60">
          {p}
        </li>
      ))}
    </ul>
  );
}
