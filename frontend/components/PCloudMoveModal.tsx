"use client";

import { useEffect, useState } from "react";
import { Folder } from "lucide-react";
import { toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, type PCloudFile } from "@/lib/api";

type Crumb = { id: string; name: string };

export default function PCloudMoveModal({
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
  const [crumbs, setCrumbs] = useState<Crumb[]>([
    { id: "0", name: "我的 pCloud" },
  ]);
  const [folders, setFolders] = useState<PCloudFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const currentId = crumbs[crumbs.length - 1].id;

  useEffect(() => {
    if (!open) {
      setCrumbs([{ id: "0", name: "我的 pCloud" }]);
      return;
    }
    let alive = true;
    setLoading(true);
    api
      .get<PCloudFile[]>(
        `/api/pcloud/files?parent_id=${encodeURIComponent(currentId)}`
      )
      .then((res) => {
        if (!alive) return;
        setFolders(res.filter((f) => f.kind === "folder"));
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

  function openFolder(f: PCloudFile) {
    setCrumbs([...crumbs, { id: f.id, name: f.name }]);
  }

  async function submit() {
    if (submitting) return;
    setSubmitting(true);
    try {
      await api.post("/api/pcloud/files/move", {
        file_ids: fileIds,
        target_folder_id: currentId,
      });
      toast.success(
        `已移動 ${fileIds.length} 個項目到 ${crumbs[crumbs.length - 1].name}`
      );
      onDone();
      onClose();
    } catch (e: any) {
      toast.error(e.message || "移動失敗");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
    >
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>移動 {fileIds.length} 個項目</DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <nav
            aria-label="路徑"
            className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground"
          >
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
          </nav>

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
                      className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-muted/50"
                    >
                      <Folder
                        className="h-4 w-4 shrink-0 text-muted-foreground"
                        aria-hidden
                      />
                      <span className="truncate text-foreground">{f.name}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <DialogFooter className="items-center gap-2 sm:justify-between">
          <div className="text-xs text-muted-foreground/70">
            目前選擇：
            <span className="ml-1 font-mono text-muted-foreground">
              {crumbs.map((c) => c.name).join(" / ")}
            </span>
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={onClose}>
              取消
            </Button>
            <Button
              size="sm"
              onClick={submit}
              disabled={submitting || !fileIds.length}
            >
              {submitting ? "移動中…" : "移動到此資料夾"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
