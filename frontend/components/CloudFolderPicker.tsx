"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "@/components/Toast";

export type CloudFolderSelection = { id: string; name: string; path: string };

type Item = { id: string; name: string; kind: string };

// PikPak's drive root is the empty string; pCloud's is "0".
const ROOT: Record<"pikpak" | "pcloud", { id: string; name: string }> = {
  pikpak: { id: "", name: "我的 PikPak" },
  pcloud: { id: "0", name: "我的 pCloud" },
};

/**
 * Inline folder browser for one cloud. Navigating into a folder selects it
 * — the current breadcrumb leaf is the selection, reported via onChange.
 * Only folders are listed (you pick a folder to scan, not a file).
 */
export default function CloudFolderPicker({
  provider,
  onChange,
}: {
  provider: "pikpak" | "pcloud";
  onChange: (sel: CloudFolderSelection) => void;
}) {
  const root = ROOT[provider];
  const [crumbs, setCrumbs] = useState<{ id: string; name: string }[]>([root]);
  const [folders, setFolders] = useState<Item[]>([]);
  const [loading, setLoading] = useState(false);

  const current = crumbs[crumbs.length - 1];

  const load = useCallback(
    async (parentId: string) => {
      setLoading(true);
      try {
        const sizeParam = provider === "pikpak" ? "&size=500" : "";
        const res = await api.get<Item[]>(
          `/api/${provider}/files?parent_id=${encodeURIComponent(
            parentId
          )}${sizeParam}`
        );
        setFolders(
          res.filter((f) => f.kind === "folder" || f.kind === "drive#folder")
        );
      } catch (e: any) {
        toast.error(e.message || "讀取資料夾失敗");
        setFolders([]);
      } finally {
        setLoading(false);
      }
    },
    [provider]
  );

  useEffect(() => {
    load(current.id);
  }, [current.id, load]);

  // Report the current folder as the selection whenever navigation changes.
  // onChange is intentionally excluded from deps: parents pass a fresh
  // closure each render, and we only want to fire on actual navigation.
  useEffect(() => {
    onChange({
      id: current.id,
      name: current.name,
      path: crumbs.map((c) => c.name).join(" / "),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [crumbs]);

  return (
    <div className="space-y-2 rounded-lg border border-white/10 bg-panel p-3">
      <div className="flex flex-wrap items-center gap-1 text-sm text-white/60">
        {crumbs.map((c, i) => (
          <span key={c.id + i} className="flex items-center gap-1">
            {i > 0 && <span className="text-white/30">/</span>}
            <button
              className="hover:text-accent"
              onClick={() => setCrumbs(crumbs.slice(0, i + 1))}
            >
              {c.name}
            </button>
          </span>
        ))}
      </div>

      <div className="max-h-[36vh] min-h-[8rem] overflow-auto rounded-md border border-white/10">
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
                  onClick={() => setCrumbs([...crumbs, { id: f.id, name: f.name }])}
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

      <div className="text-xs text-white/40">
        將掃描：
        <span className="ml-1 font-mono text-white/70">
          {crumbs.map((c) => c.name).join(" / ")}
        </span>
      </div>
    </div>
  );
}
