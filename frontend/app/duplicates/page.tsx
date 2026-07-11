"use client";

import { useEffect, useMemo, useState } from "react";
import { CopyCheck, Folder } from "lucide-react";
import CloudFolderPicker, {
  type CloudFolderSelection,
} from "@/components/CloudFolderPicker";
import { confirmDialog, toast } from "@/components/Toast";
import { EmptyState } from "@/components/shared/EmptyState";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  api,
  streamNdjson,
  type PCloudStatus,
  type PikPakStatus,
} from "@/lib/api";

type DupFile = {
  path: string;
  id: string;
  is_folder: boolean;
};

type DupRow = {
  code: string;
  pikpak_files: DupFile[];
  pcloud_files: DupFile[];
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

type Side = "pikpak" | "pcloud";

export default function DuplicatesPage() {
  const [pikpakSel, setPikpakSel] = useState<CloudFolderSelection | null>(null);
  const [pcloudSel, setPcloudSel] = useState<CloudFolderSelection | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ pikpak?: string; pcloud?: string }>(
    {}
  );
  const [result, setResult] = useState<DupResult | null>(null);
  // Selected file entries, keyed "side:id".
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);

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
    setSelected(new Set());
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

  function toggle(side: Side, id: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      const key = `${side}:${id}`;
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  function selectAllOn(side: Side, on: boolean) {
    if (!result) return;
    setSelected((prev) => {
      const next = new Set(prev);
      for (const d of result.duplicates) {
        const files = side === "pikpak" ? d.pikpak_files : d.pcloud_files;
        for (const f of files) {
          const key = `${side}:${f.id}`;
          if (on) next.add(key);
          else next.delete(key);
        }
      }
      return next;
    });
  }

  const counts = useMemo(() => {
    let pikpak = 0;
    let pcloud = 0;
    for (const key of Array.from(selected)) {
      if (key.startsWith("pikpak:")) pikpak++;
      else pcloud++;
    }
    return { pikpak, pcloud };
  }, [selected]);

  async function deleteSelected(side: Side) {
    if (!result) return;
    const ids = Array.from(selected)
      .filter((k) => k.startsWith(`${side}:`))
      .map((k) => k.slice(side.length + 1));
    if (!ids.length) return;
    const label = side === "pikpak" ? "PikPak" : "pCloud";
    const ok = await confirmDialog(
      `把選取的 ${ids.length} 個項目移到 ${label} 垃圾桶？\n(只動 ${label} 這一側,另一邊的檔案保留)`
    );
    if (!ok) return;
    setDeleting(true);
    try {
      await api.post(`/api/${side}/files/trash`, { ids });
      toast.success(`已把 ${ids.length} 個項目移到 ${label} 垃圾桶`);
      const removed = new Set(ids);
      // Drop deleted entries locally; a code stops being a duplicate row
      // only if one side no longer has any hit.
      setResult((r) =>
        r
          ? {
              ...r,
              duplicates: r.duplicates
                .map((d) => ({
                  ...d,
                  pikpak_files:
                    side === "pikpak"
                      ? d.pikpak_files.filter((f) => !removed.has(f.id))
                      : d.pikpak_files,
                  pcloud_files:
                    side === "pcloud"
                      ? d.pcloud_files.filter((f) => !removed.has(f.id))
                      : d.pcloud_files,
                }))
                .filter((d) => d.pikpak_files.length && d.pcloud_files.length),
            }
          : r
      );
      setSelected((prev) => {
        const next = new Set(prev);
        for (const id of ids) next.delete(`${side}:${id}`);
        return next;
      });
    } catch (e: any) {
      toast.error(`刪除失敗:${e.message}`);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">
          跨雲重複番號比對
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          各選一個資料夾,遞迴掃描整個子樹,列出在 PikPak 與 pCloud
          兩邊都存在的番號。掃描為唯讀;勾選後可把其中一邊的重複檔移到該雲端的垃圾桶。
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
          <div className="text-sm font-medium text-foreground/80">
            PikPak 路徑
          </div>
          <CloudFolderPicker provider="pikpak" onChange={setPikpakSel} />
        </div>
        <div className="space-y-2">
          <div className="text-sm font-medium text-foreground/80">
            pCloud 路徑
          </div>
          <CloudFolderPicker provider="pcloud" onChange={setPcloudSel} />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={run} disabled={running || !bothReady}>
          {running ? "比對中…" : "開始比對"}
        </Button>
        {(progress.pikpak || progress.pcloud) && (
          <div className="text-xs text-muted-foreground">
            {progress.pikpak && <div>PikPak:{progress.pikpak}</div>}
            {progress.pcloud && <div>pCloud:{progress.pcloud}</div>}
          </div>
        )}
      </div>

      {result && (
        <ResultPanel
          result={result}
          selected={selected}
          onToggle={toggle}
          onSelectAll={selectAllOn}
          onCopy={copyCodes}
        />
      )}

      {(counts.pikpak > 0 || counts.pcloud > 0) && (
        <div className="sticky bottom-3 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card/95 px-4 py-3 shadow-lg backdrop-blur">
          <span className="text-sm text-foreground/70">
            已選 PikPak {counts.pikpak} ・ pCloud {counts.pcloud}
          </span>
          {counts.pikpak > 0 && (
            <Button
              variant="outline"
              size="sm"
              className="border-red-500/40 bg-red-500/10 text-red-300 hover:bg-red-500/20 hover:text-red-300"
              onClick={() => deleteSelected("pikpak")}
              disabled={deleting}
            >
              刪除已選 PikPak 檔案
            </Button>
          )}
          {counts.pcloud > 0 && (
            <Button
              variant="outline"
              size="sm"
              className="border-red-500/40 bg-red-500/10 text-red-300 hover:bg-red-500/20 hover:text-red-300"
              onClick={() => deleteSelected("pcloud")}
              disabled={deleting}
            >
              刪除已選 pCloud 檔案
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setSelected(new Set())}
            disabled={deleting}
          >
            清除選取
          </Button>
        </div>
      )}
    </div>
  );
}

function ResultPanel({
  result,
  selected,
  onToggle,
  onSelectAll,
  onCopy,
}: {
  result: DupResult;
  selected: Set<string>;
  onToggle: (side: Side, id: string, on: boolean) => void;
  onSelectAll: (side: Side, on: boolean) => void;
  onCopy: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground/70">
        <span>
          <span className="text-muted-foreground">PikPak 番號</span>{" "}
          {result.pikpak_codes}
          <span className="ml-1 text-muted-foreground/60">
            ({result.pikpak_items} 項)
          </span>
        </span>
        <span>
          <span className="text-muted-foreground">pCloud 番號</span>{" "}
          {result.pcloud_codes}
          <span className="ml-1 text-muted-foreground/60">
            ({result.pcloud_items} 項)
          </span>
        </span>
        <span className="font-medium text-primary">
          重複 {result.duplicate_count}
        </span>
        {(result.pikpak_partial || result.pcloud_partial) && (
          <span className="text-amber-300/80">(已達掃描上限,結果為部分)</span>
        )}
        {result.duplicate_count > 0 && (
          <button
            onClick={onCopy}
            className="ml-auto rounded border border-border px-2 py-0.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"
          >
            複製番號清單
          </button>
        )}
      </div>

      {result.duplicate_count === 0 ? (
        <EmptyState icon={CopyCheck} title="沒有發現跨雲重複的番號" />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-40 px-3 text-xs uppercase tracking-wide">
                  番號
                </TableHead>
                <TableHead className="px-3 text-xs uppercase tracking-wide">
                  <span className="mr-2">PikPak 路徑</span>
                  <button
                    className="rounded border border-border px-1.5 py-0.5 text-[10px] normal-case text-muted-foreground transition hover:bg-muted hover:text-foreground"
                    onClick={() => onSelectAll("pikpak", true)}
                  >
                    全選
                  </button>
                </TableHead>
                <TableHead className="px-3 text-xs uppercase tracking-wide">
                  <span className="mr-2">pCloud 路徑</span>
                  <button
                    className="rounded border border-border px-1.5 py-0.5 text-[10px] normal-case text-muted-foreground transition hover:bg-muted hover:text-foreground"
                    onClick={() => onSelectAll("pcloud", true)}
                  >
                    全選
                  </button>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {result.duplicates.map((d) => (
                <TableRow key={d.code} className="align-top">
                  <TableCell className="px-3 align-top font-mono font-medium text-foreground/90">
                    {d.code}
                  </TableCell>
                  <TableCell className="px-3 align-top">
                    <FileList
                      side="pikpak"
                      files={d.pikpak_files}
                      selected={selected}
                      onToggle={onToggle}
                    />
                  </TableCell>
                  <TableCell className="px-3 align-top">
                    <FileList
                      side="pcloud"
                      files={d.pcloud_files}
                      selected={selected}
                      onToggle={onToggle}
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

function FileList({
  side,
  files,
  selected,
  onToggle,
}: {
  side: Side;
  files: DupFile[];
  selected: Set<string>;
  onToggle: (side: Side, id: string, on: boolean) => void;
}) {
  if (!files.length) return <span className="text-muted-foreground/50">—</span>;
  return (
    <ul className="space-y-0.5">
      {files.map((f) => {
        const key = `${side}:${f.id}`;
        return (
          <li key={key} className="flex items-start gap-1.5">
            <Checkbox
              className="mt-0.5"
              checked={selected.has(key)}
              onCheckedChange={(v) => onToggle(side, f.id, v === true)}
              aria-label={`選取 ${f.path}`}
            />
            <span className="break-all font-mono text-xs text-muted-foreground">
              {f.is_folder && (
                <Folder
                  className="mr-1 inline-block h-3.5 w-3.5 align-[-2px]"
                  aria-hidden
                />
              )}
              {f.path}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
