"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  FileText,
  Folder,
  FolderOutput,
  MoveRight,
  Pencil,
  SkipForward,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import {
  api,
  streamNdjson,
  type PresenceDetail,
  type PresenceStatus,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import type { SetMsg } from "./types";

type ReorgProgress = {
  current: number;
  source: string;
  kind: "folder" | "file";
  action: "move" | "rename" | "flatten" | "dedupe" | "skip" | "error";
  target: string | null;
  reason: string | null;
  section?: "migrate" | "cleanup";
  context?: string;
};

type ReorgResult = {
  total: number;
  moved: number;
  renamed: number;
  flattened: number;
  deduped: number;
  skipped: number;
  errors: number;
  dry_run: boolean;
};

const REORG_ACTION: Record<
  ReorgProgress["action"],
  { text: string; cls: string; icon: LucideIcon }
> = {
  move: { text: "搬移", cls: "text-blue-300", icon: MoveRight },
  rename: { text: "改名", cls: "text-cyan-300", icon: Pencil },
  flatten: { text: "攤平", cls: "text-emerald-300", icon: FolderOutput },
  dedupe: { text: "去重", cls: "text-purple-300", icon: Trash2 },
  skip: { text: "略過", cls: "text-muted-foreground", icon: SkipForward },
  error: { text: "失敗", cls: "text-red-300", icon: X },
};

const REORG_REASON: Record<string, string> = {
  no_code: "無法辨識番號",
  no_tracked_match: "沒有對應的追蹤分類",
  conflict: "目標已有同名資料夾",
  bad_target: "解析目標路徑失敗",
  resolve_failed: "查詢 JavBus 失敗",
  already_clean: "已經規範化",
  duplicate: "重複（保留較大者）",
};

export default function ReorganizeSection({ setMsg }: { setMsg: SetMsg }) {
  const [presence, setPresence] = useState<PresenceDetail | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showDebug, setShowDebug] = useState(false);
  const [open, setOpen] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [busy, setBusy] = useState(false);
  const [total, setTotal] = useState(0);
  const [progress, setProgress] = useState<ReorgProgress[]>([]);
  const [result, setResult] = useState<ReorgResult | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [cleanupTargets, setCleanupTargets] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const loadPresence = useCallback(async () => {
    try {
      const p = await api.get<PresenceDetail>("/api/pikpak/presence/detail");
      setPresence(p);
    } catch {
      setPresence(null);
    }
  }, []);

  useEffect(() => {
    loadPresence();
  }, [loadPresence]);

  async function refreshIndex() {
    setRefreshing(true);
    try {
      await api.post<PresenceStatus>("/api/pikpak/presence/refresh");
      const p = await api.get<PresenceDetail>(
        "/api/pikpak/presence/detail"
      );
      setPresence(p);
      setMsg({
        kind: "ok",
        text: `PikPak 索引已重建（共 ${p.size} 個番號）`,
      });
    } catch (e: any) {
      setMsg({ kind: "err", text: `重建索引失敗：${e.message}` });
    } finally {
      setRefreshing(false);
    }
  }

  async function runReorg() {
    if (!dryRun) {
      if (
        !confirm(
          "正式整理會把 AVBT/已完成 下的番號資料夾搬到對應的 series / star 子資料夾。確定？"
        )
      )
        return;
    }
    setBusy(true);
    setErrMsg(null);
    setResult(null);
    setProgress([]);
    setTotal(0);
    setCleanupTargets([]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const wasDryRun = dryRun;
    try {
      await streamNdjson(
        "/api/pikpak/reorganize",
        { dry_run: wasDryRun },
        (event) => {
          if (event.type === "start") {
            setTotal(event.total ?? 0);
            setCleanupTargets(event.cleanup_targets ?? []);
          }
          else if (event.type === "progress")
            setProgress((prev) => [...prev, event]);
          else if (event.type === "done") setResult(event.result);
          else if (event.type === "error") setErrMsg(event.message);
        },
        ctrl.signal
      );
      if (!wasDryRun) {
        loadPresence();
      }
    } catch (e: any) {
      if (e.name !== "AbortError") setErrMsg(e.message);
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  function close() {
    if (busy) return;
    setOpen(false);
    setProgress([]);
    setResult(null);
    setErrMsg(null);
    setTotal(0);
    setDryRun(true);
  }

  const percent = total ? Math.round((progress.length / total) * 100) : 0;
  const recent = progress.slice(-10).reverse();

  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h2 className="text-lg font-semibold">PikPak 資料夾結構整理</h2>
      <p className="text-xs text-muted-foreground/80">
        新下載會自動依追蹤的系列 / 女優分類，歸檔到{" "}
        <span className="font-mono">AVBT/&lt;類別&gt;/&lt;名稱&gt;/&lt;番號&gt;</span>
        。下方可重建索引、或把舊的扁平歸檔搬到新結構。
      </p>
      {presence ? (
        <div className="space-y-1 text-xs text-muted-foreground">
          <div>
            索引狀態：{presence.ready ? (
              <span className="text-emerald-300">已建立</span>
            ) : (
              <span className="text-amber-300">尚未建立</span>
            )}
            ・收錄 <span className="font-mono">{presence.size}</span> 個番號
          </div>
          <div>
            最後建立：
            {presence.built_at
              ? new Date(presence.built_at + "Z").toLocaleString()
              : "從未"}
            ・TTL {presence.ttl_seconds}s
          </div>
          {presence.last_error && (
            <div className="inline-flex items-center gap-1 text-amber-300/80">
              <TriangleAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
              {presence.last_error}
            </div>
          )}
        </div>
      ) : (
        <div className="text-sm text-muted-foreground/70">載入中…</div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={refreshIndex} disabled={refreshing}>
          {refreshing ? "重建索引中…" : "重建索引"}
        </Button>
        <Button
          variant="outline"
          onClick={() => setOpen(true)}
          title="掃 AVBT 根目錄 + 舊版「已完成」,依番號對應的追蹤分類搬到 AVBT/<類別>/<名稱>/,並做命名正規化"
        >
          整理 PikPak 資料夾…
        </Button>
        <Button variant="ghost" onClick={() => setShowDebug((v) => !v)}>
          {showDebug ? "收合偵錯" : "看索引偵錯"}
        </Button>
      </div>

      {showDebug && presence && (
        <div className="space-y-2 rounded-md border border-border bg-background/60 p-3 text-xs">
          <div className="font-semibold text-foreground/70">
            掃描的根目錄(共 {presence.roots.length})
          </div>
          {presence.roots.length === 0 ? (
            <div className="text-muted-foreground/70">
              索引還沒建立過。請點上方「重建索引」。
            </div>
          ) : (
            <ul className="space-y-0.5 font-mono">
              {presence.roots.map((r) => (
                <li key={r.path} className="flex flex-wrap gap-2">
                  <span className="text-foreground/80">{r.path}</span>
                  <span className="text-muted-foreground/70">
                    leaves={r.leaves} · codes={r.codes}
                    {r.unrecognized > 0 && (
                      <span className="text-amber-300">
                        {" · unrecognized=" + r.unrecognized}
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className="pt-1 font-semibold text-foreground/70">
            無法辨識為番號的資料夾名(共 {presence.unrecognized_total}
            ,最多顯示 50 個)
          </div>
          {presence.unrecognized.length === 0 ? (
            <div className="text-muted-foreground/70">
              所有掃到的葉節點都成功辨識為番號。
            </div>
          ) : (
            <ul className="max-h-64 space-y-0.5 overflow-auto font-mono">
              {presence.unrecognized.map((u, i) => (
                <li key={i} className="text-amber-200/80">
                  <span className="text-muted-foreground/70">{u.parent}/</span>
                  {u.name}
                </li>
              ))}
            </ul>
          )}
          <div className="pt-1 text-muted-foreground/70">
            說明:索引只會掃 <span className="font-mono">AVBT/star</span>、
            <span className="font-mono">/series</span>、
            <span className="font-mono">/studio</span>、
            <span className="font-mono">/label</span>、
            <span className="font-mono">/director</span> 下的
            <span className="font-mono">&lt;name&gt;/&lt;code&gt;</span>
            ,加上舊版 <span className="font-mono">AVBT/已完成/&lt;code&gt;</span>
            。若你的檔案在其他路徑(例如直接放在 AVBT/),會被當成「找不到」。
          </div>
        </div>
      )}

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12"
          onClick={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div className="w-full max-w-xl space-y-4 rounded-xl border border-border bg-card p-5">
            <div className="flex items-center">
              <h2 className="text-lg font-semibold">重新整理 PikPak 結構</h2>
              <button
                className="ml-auto text-muted-foreground transition hover:text-foreground"
                onClick={close}
                aria-label="關閉"
              >
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>

            <div className="space-y-1 text-xs text-muted-foreground/80">
              <p>
                <span className="rounded bg-blue-500/15 px-1 text-[10px] text-blue-300">
                  搬移
                </span>{" "}
                掃 <span className="font-mono">AVBT/</span> 根目錄裡的散檔 / BT 命名資料夾,加上舊版{" "}
                <span className="font-mono">AVBT/已完成</span> 的內容,依番號對應的追蹤分類搬到（優先序 series &gt; director &gt; label &gt; studio &gt; star）
                <span className="font-mono">
                  AVBT/&lt;類別&gt;/&lt;名稱&gt;/&lt;番號&gt;
                </span>
                。
              </p>
              <p>
                <span className="rounded bg-purple-500/15 px-1 text-[10px] text-purple-300">
                  清理
                </span>{" "}
                走訪每個追蹤分類的目的資料夾：髒名字改成 canonical
                <span className="font-mono">（&lt;番號&gt; / &lt;番號&gt;.ext）</span>
                ；wrapper 資料夾裡有 ≥1 個主檔（≥300 MB）就攤平，取最大的影片到父層、其餘垃圾與空殼丟回收筒；同番號重複時保留較大者。
              </p>
            </div>

            <div className="flex items-center gap-2">
              <Checkbox
                id="reorg-dry-run"
                checked={dryRun}
                onCheckedChange={(v) => setDryRun(v === true)}
                disabled={busy}
              />
              <Label htmlFor="reorg-dry-run" className="text-sm font-normal">
                只預覽（不實際搬移）
              </Label>
            </div>

            {cleanupTargets.length > 0 && (
              <details className="rounded-md border border-border bg-background/60 px-3 py-2 text-xs">
                <summary className="cursor-pointer text-foreground/70">
                  清理階段會掃 {cleanupTargets.length} 個資料夾
                </summary>
                <ul className="mt-2 max-h-40 space-y-0.5 overflow-y-auto">
                  {cleanupTargets.map((p) => (
                    <li key={p} className="truncate font-mono text-muted-foreground/80">
                      {p}
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-muted-foreground/70">
                  路徑不對？檢查 .env 的{" "}
                  <span className="font-mono">PIKPAK_{"<KIND>"}_FOLDER</span>{" "}
                  設定（例如{" "}
                  <span className="font-mono">PIKPAK_SERIES_FOLDER</span>），重啟後重試。
                </p>
              </details>
            )}

            {errMsg && (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {errMsg}
              </div>
            )}

            {(busy || result) && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>
                    {progress.length} / {total} ({percent}%)
                    {result?.dry_run && " ・ 預覽模式"}
                  </span>
                  <span>
                    搬 {progress.filter((p) => p.action === "move").length} ／
                    名 {progress.filter((p) => p.action === "rename").length} ／
                    平 {progress.filter((p) => p.action === "flatten").length} ／
                    去 {progress.filter((p) => p.action === "dedupe").length} ／
                    略 {progress.filter((p) => p.action === "skip").length} ／
                    錯 {progress.filter((p) => p.action === "error").length}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded bg-muted">
                  <div
                    className="h-full bg-primary transition-[width]"
                    style={{ width: `${percent}%` }}
                  />
                </div>
                <ul className="max-h-56 overflow-y-auto rounded-md border border-border bg-background/60 p-2 text-xs">
                  {recent.length === 0 && (
                    <li className="text-muted-foreground/70">等待第一筆…</li>
                  )}
                  {recent.map((p) => {
                    const lbl = REORG_ACTION[p.action];
                    const ActionIcon = lbl.icon;
                    const KindIcon = p.kind === "file" ? FileText : Folder;
                    const reasonTxt =
                      p.reason && REORG_REASON[p.reason]
                        ? `（${REORG_REASON[p.reason]}）`
                        : p.reason
                        ? `（${p.reason}）`
                        : "";
                    const sectionTag =
                      p.section === "cleanup" ? (
                        <span className="rounded bg-purple-500/15 px-1 text-[10px] text-purple-300">
                          清理
                        </span>
                      ) : p.section === "migrate" ? (
                        <span className="rounded bg-blue-500/15 px-1 text-[10px] text-blue-300">
                          搬移
                        </span>
                      ) : null;
                    return (
                      <li
                        key={`${p.current}-${p.source}`}
                        className="flex items-baseline gap-2 py-0.5"
                      >
                        {sectionTag}
                        <span className={`inline-flex items-center gap-1 ${lbl.cls}`}>
                          <ActionIcon className="h-3 w-3 shrink-0" aria-hidden />
                          {lbl.text}
                          {reasonTxt}
                        </span>
                        <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground">
                          <KindIcon className="h-3 w-3 shrink-0" aria-hidden />
                          <span className="truncate">{p.source}</span>
                        </span>
                        {p.target && (
                          <>
                            <span className="text-muted-foreground/50">→</span>
                            <span className="truncate font-mono text-primary">
                              {p.target}
                            </span>
                          </>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {result && (
              <div className="space-y-1 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
                <div>
                  共 <strong>{result.total}</strong> 個項目
                  {result.dry_run && (
                    <span className="ml-2 text-amber-300/80">
                      （僅預覽，未修改）
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1.5 text-blue-300">
                  <MoveRight className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  搬移 {result.moved}
                </div>
                <div className="flex items-center gap-1.5 text-cyan-300">
                  <Pencil className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  改名 {result.renamed}
                </div>
                <div className="flex items-center gap-1.5 text-emerald-300">
                  <FolderOutput className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  攤平 {result.flattened}
                </div>
                <div className="flex items-center gap-1.5 text-purple-300">
                  <Trash2 className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  去重 {result.deduped}
                </div>
                <div className="flex items-center gap-1.5 text-muted-foreground">
                  <SkipForward className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  略過 {result.skipped}
                </div>
                {result.errors > 0 && (
                  <div className="flex items-center gap-1.5 text-red-300">
                    <X className="h-3.5 w-3.5 shrink-0" aria-hidden />
                    失敗 {result.errors}
                  </div>
                )}
              </div>
            )}

            <div className="flex justify-end gap-2">
              {busy ? (
                <Button
                  variant="outline"
                  onClick={() => abortRef.current?.abort()}
                >
                  取消
                </Button>
              ) : (
                <>
                  <Button variant="ghost" onClick={close}>
                    關閉
                  </Button>
                  <Button onClick={runReorg}>
                    {dryRun ? "預覽" : "執行"}
                  </Button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
