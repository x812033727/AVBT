"use client";

import { useRef, useState } from "react";
import {
  FileText,
  Folders,
  Layers,
  Merge,
  MoveRight,
  Pencil,
  SkipForward,
  Trash2,
  X,
  type LucideIcon,
} from "lucide-react";
import { confirmDialog } from "@/components/Toast";
import { streamNdjson } from "@/lib/api";
import { Button } from "@/components/ui/button";

// NDJSON events the three hygiene sweep engines share:
// progress/skip carry what happened to one item, done carries the
// summary. Field names differ slightly per engine (a plain "target" path
// for a trash/rename, a bare child name + "to" folder for a move, a
// "keeps" count for a merge) — the card below renders whichever subset
// is present rather than assuming one shape.
type SweepEvent = {
  type: "progress" | "skip" | "error" | "done";
  action?: string;
  target?: string;
  to?: string;
  reason?: string;
  keeps?: number;
  message?: string;
  result?: Record<string, number | boolean>;
};

type SweepLine = {
  key: number;
  kind: "progress" | "skip";
  action?: string;
  target?: string;
  to?: string;
  reason?: string;
  keeps?: number;
};

const ACTION_LABEL: Record<string, { text: string; icon: LucideIcon; cls: string }> = {
  trash: { text: "丟垃圾桶", icon: Trash2, cls: "text-red-300" },
  rename: { text: "改名", icon: Pencil, cls: "text-cyan-300" },
  merge: { text: "合併", icon: Merge, cls: "text-purple-300" },
  move: { text: "搬移", icon: MoveRight, cls: "text-blue-300" },
};

const REASON_LABEL: Record<string, string> = {
  in_flight: "傳輸中，暫緩",
  name_taken: "目標已有同名檔案，留給人工判斷",
  move_settling: "等待搬移沉澱確認（30 分鐘）",
  emptied_shell: "已清空的殼夾",
};

type ResultField = {
  key: string;
  label: string;
  icon: LucideIcon;
  cls: string;
};

type SweepConfig = {
  key: string;
  title: string;
  description: string;
  endpoint: string;
  fields: ResultField[];
};

const SWEEPS: SweepConfig[] = [
  {
    key: "dup-copies",
    title: "重複副本清理",
    description:
      "升級下載留下的 CODE(1).mp4 殘留副本：保留最大的一份，其餘丟垃圾桶，下一輪再把倖存者改名回 CODE.mp4。真正的分集（CODE_1 / CODE_2）不受影響。",
    endpoint: "/api/pikpak/dup-copies/sweep",
    fields: [
      { key: "scanned", label: "掃描", icon: FileText, cls: "text-muted-foreground" },
      { key: "trashed", label: "丟垃圾桶", icon: Trash2, cls: "text-red-300" },
      { key: "renamed", label: "改名", icon: Pencil, cls: "text-cyan-300" },
      { key: "errors", label: "錯誤", icon: X, cls: "text-red-300" },
    ],
  },
  {
    key: "folder-twins",
    title: "系列雙胞胎合併",
    description:
      "同一系列被重複建立成兩個資料夾（名稱漂移或建立競速）：檔案數較多者留下當贏家，其餘搬進去；空殼要等 30 分鐘搬移沉澱確認後才清掉。",
    endpoint: "/api/pikpak/folder-twins/merge",
    fields: [
      { key: "groups", label: "組數", icon: Layers, cls: "text-muted-foreground" },
      { key: "moved", label: "搬移", icon: MoveRight, cls: "text-blue-300" },
      { key: "skipped", label: "跳過", icon: SkipForward, cls: "text-muted-foreground" },
      { key: "shells", label: "殼夾", icon: Folders, cls: "text-purple-300" },
      { key: "errors", label: "錯誤", icon: X, cls: "text-red-300" },
    ],
  },
  {
    key: "series-junk",
    title: "系列垃圾清除",
    description:
      "BT 隨附的廣告片段散落在系列資料夾裡（finalize 只清番號資料夾內部，管不到這裡）：判定為垃圾的檔案丟垃圾桶，可從回收筒復原。",
    endpoint: "/api/pikpak/series-junk/purge",
    fields: [
      { key: "scanned", label: "掃描", icon: FileText, cls: "text-muted-foreground" },
      { key: "trashed", label: "丟垃圾桶", icon: Trash2, cls: "text-red-300" },
    ],
  },
];

function SweepCard({ config }: { config: SweepConfig }) {
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState<SweepLine[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [result, setResult] = useState<Record<string, number | boolean> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const counterRef = useRef(0);

  async function run(preview: boolean) {
    if (!preview) {
      const ok = await confirmDialog(
        "檔案會進 PikPak 垃圾桶(可復原),確定執行?"
      );
      if (!ok) return;
    }
    setBusy(true);
    setLines([]);
    setErrors([]);
    setResult(null);
    counterRef.current = 0;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamNdjson(
        `${config.endpoint}?dry_run=${preview}`,
        {},
        (event: SweepEvent) => {
          if (event.type === "progress" || event.type === "skip") {
            const kind = event.type;
            const key = counterRef.current++;
            setLines((prev) => [
              ...prev,
              {
                key,
                kind,
                action: event.action,
                target: event.target,
                to: event.to,
                reason: event.reason,
                keeps: event.keeps,
              },
            ]);
          } else if (event.type === "error") {
            setErrors((prev) => [...prev, event.message ?? "未知錯誤"]);
          } else if (event.type === "done") {
            setResult(event.result ?? null);
          }
        },
        ctrl.signal
      );
    } catch (e: any) {
      if (e.name !== "AbortError") {
        setErrors((prev) => [...prev, e.message ?? String(e)]);
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  const recent = lines.slice(-8);
  const dryRunDone = result ? Boolean(result.dry_run) : false;

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-background/40 p-3">
      <div>
        <h3 className="text-sm font-semibold">{config.title}</h3>
        <p className="mt-1 text-xs text-muted-foreground/80">
          {config.description}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => run(true)}
          disabled={busy}
        >
          預覽
        </Button>
        <Button size="sm" onClick={() => run(false)} disabled={busy}>
          執行
        </Button>
        {busy && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => abortRef.current?.abort()}
          >
            取消
          </Button>
        )}
      </div>

      {(busy || lines.length > 0) && (
        <ul className="max-h-48 space-y-0.5 overflow-y-auto rounded-md border border-border bg-background/60 p-2 text-xs">
          {recent.length === 0 && (
            <li className="text-muted-foreground/70">掃描中…</li>
          )}
          {recent.map((l) => {
            const lbl = l.kind === "progress" && l.action ? ACTION_LABEL[l.action] : undefined;
            const Icon = l.kind === "skip" ? SkipForward : lbl?.icon ?? FileText;
            const cls =
              l.kind === "skip" ? "text-muted-foreground/70" : lbl?.cls ?? "text-muted-foreground";
            const text = l.kind === "skip" ? "略過" : lbl?.text ?? l.action ?? "處理";
            const reasonTxt = l.reason
              ? `（${REASON_LABEL[l.reason] ?? l.reason}）`
              : "";
            return (
              <li key={l.key} className="flex items-baseline gap-1.5 py-0.5">
                <span className={`inline-flex shrink-0 items-center gap-1 ${cls}`}>
                  <Icon className="h-3 w-3 shrink-0" aria-hidden />
                  {text}
                  {reasonTxt}
                </span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground">
                  {l.target}
                  {typeof l.keeps === "number" && ` （保留 ${l.keeps} 筆）`}
                </span>
                {l.to && (
                  <>
                    <span className="shrink-0 text-muted-foreground/50">→</span>
                    <span className="shrink-0 truncate font-mono text-primary">
                      {l.to}
                    </span>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {errors.length > 0 && (
        <div className="max-h-28 space-y-1 overflow-y-auto rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1.5 text-xs text-red-300">
          {errors.map((e, i) => (
            <div key={i} className="font-mono">
              • {e}
            </div>
          ))}
        </div>
      )}

      {result && (
        <div className="space-y-1 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
          {dryRunDone && (
            <div className="text-amber-300/80">（僅預覽，未修改）</div>
          )}
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            {config.fields.map((f) => {
              const v = result[f.key];
              if (f.key === "errors" && !v) return null;
              const Icon = f.icon;
              return (
                <span key={f.key} className={`inline-flex items-center gap-1 ${f.cls}`}>
                  <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  {f.label} {typeof v === "number" ? v : 0}
                </span>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

export default function HygieneSweepsSection() {
  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h2 className="text-lg font-semibold">清理引擎</h2>
      <p className="text-xs text-muted-foreground/80">
        三個獨立的資料夾衛生掃描,各自「預覽」看會動到什麼、確認沒問題再「執行」。
        所有異動都進 PikPak 垃圾桶,可在 30 天內復原。
      </p>
      <div className="grid gap-3 md:grid-cols-3">
        {SWEEPS.map((cfg) => (
          <SweepCard key={cfg.key} config={cfg} />
        ))}
      </div>
    </section>
  );
}
