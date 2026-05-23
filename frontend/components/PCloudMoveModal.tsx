"use client";

import { useEffect, useState } from "react";
import { api, type PCloudFile } from "@/lib/api";
import { toast } from "@/components/Toast";

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

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl rounded-lg border border-white/10 bg-panel shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <div className="text-sm font-medium">移動 {fileIds.length} 個項目</div>
          <button onClick={onClose} className="text-white/60 hover:text-white">
            ✕
          </button>
        </div>

        <div className="space-y-3 p-4">
          <div className="flex flex-wrap items-center gap-1 text-sm text-white/60">
            {crumbs.map((c, i) => (
              <span key={c.id + i} className="flex items-center gap-1">
                {i > 0 && <span className="text-white/30">/</span>}
                <button
                  className="hover:text-accent"
                  onClick={() => gotoCrumb(i)}
                >
                  {c.name}
                </button>
              </span>
            ))}
          </div>

          <div className="max-h-[40vh] min-h-[10rem] overflow-auto rounded-md border border-white/10">
            {loading ? (
              <div className="px-3 py-6 text-center text-sm text-white/40">
                載入中…
              </div>
            ) : folders.length === 0 ? (
              <div className="px-3 py-6 text-center text-sm text-white/40">
                此目錄沒有子資料夾
              </div>
            ) : (
              <ul className="divide-y divide-white/5 text-sm">
                {folders.map((f) => (
                  <li key={f.id}>
                    <button
                      onClick={() => openFolder(f)}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-white/5"
                    >
                      <span>📁</span>
                      <span className="truncate text-white/90">{f.name}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-white/10 px-4 py-3">
          <div className="text-xs text-white/40">
            目前選擇：
            <span className="ml-1 font-mono text-white/70">
              {crumbs.map((c) => c.name).join(" / ")}
            </span>
          </div>
          <div className="flex gap-2">
            <button className="btn-ghost" onClick={onClose}>
              取消
            </button>
            <button
              className="btn-primary"
              onClick={submit}
              disabled={submitting || !fileIds.length}
            >
              {submitting ? "移動中…" : "移動到此資料夾"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
