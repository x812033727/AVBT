"use client";

import { useEffect, useState } from "react";
import { Folder } from "lucide-react";
import { api, type PikPakFile } from "@/lib/api";
import { toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Crumb = { id: string; name: string };

export default function MoveModal({
  open,
  fileIds,
  onClose,
  onDone,
}: {
  open: boolean;
  fileIds: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [crumbs, setCrumbs] = useState<Crumb[]>([{ id: "", name: "我的雲盤" }]);
  const [folders, setFolders] = useState<PikPakFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const currentId = crumbs[crumbs.length - 1].id;

  useEffect(() => {
    if (!open) {
      setCrumbs([{ id: "", name: "我的雲盤" }]);
      return;
    }
    let alive = true;
    setLoading(true);
    api
      .get<PikPakFile[]>(
        `/api/pikpak/files?parent_id=${encodeURIComponent(currentId)}&size=500`
      )
      .then((res) => {
        if (!alive) return;
        setFolders(res.filter((f) => f.kind === "drive#folder"));
      })
      .catch((e: any) => {
        if (!alive) return;
        toast.error(e.message || "讀取資料夾失敗");
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [open, currentId]);

  function gotoCrumb(idx: number) {
    setCrumbs(crumbs.slice(0, idx + 1));
  }

  function openFolder(f: PikPakFile) {
    setCrumbs([...crumbs, { id: f.id, name: f.name }]);
  }

  async function submit() {
    if (submitting) return;
    setSubmitting(true);
    try {
      await api.post("/api/pikpak/files/move", {
        file_ids: fileIds,
        target_folder_id: currentId,
      });
      toast.success(`已移動 ${fileIds.length} 個檔案到 ${crumbs[crumbs.length - 1].name}`);
      onDone();
      onClose();
    } catch (e: any) {
      toast.error(e.message || "移動失敗");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="text-base">
            移動 {fileIds.length} 個檔案
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground">
            {crumbs.map((c, i) => (
              <span key={c.id + i} className="flex items-center gap-1">
                {i > 0 && <span className="text-muted-foreground/40">/</span>}
                <button
                  type="button"
                  className="transition-colors hover:text-primary"
                  onClick={() => gotoCrumb(i)}
                >
                  {c.name}
                </button>
              </span>
            ))}
          </div>

          <div className="max-h-[40vh] min-h-[10rem] overflow-auto rounded-md border border-border">
            {loading ? (
              <div className="px-3 py-6 text-center text-sm text-muted-foreground/70">
                載入中…
              </div>
            ) : folders.length === 0 ? (
              <div className="px-3 py-6 text-center text-sm text-muted-foreground/70">
                此目錄沒有子資料夾
              </div>
            ) : (
              <ul className="divide-y divide-border/50 text-sm">
                {folders.map((f) => (
                  <li key={f.id}>
                    <button
                      type="button"
                      onClick={() => openFolder(f)}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-muted"
                    >
                      <Folder className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                      <span className="truncate text-foreground">{f.name}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="text-xs text-muted-foreground/70">
            目前選擇：
            <span className="ml-1 font-mono text-muted-foreground">
              {crumbs.map((c) => c.name).join(" / ")}
            </span>
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button onClick={submit} disabled={submitting || !fileIds.length}>
            {submitting ? "移動中…" : "移動到此資料夾"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
