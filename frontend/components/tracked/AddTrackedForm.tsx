"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { api, type TrackedKind, type TrackedListing } from "@/lib/api";

const ADD_KINDS: { value: TrackedKind; label: string }[] = [
  { value: "star", label: "女優" },
  { value: "studio", label: "製作商" },
  { value: "label", label: "發行商" },
  { value: "series", label: "系列" },
  { value: "director", label: "導演" },
  { value: "genre", label: "類別" },
];

// 手動新增追蹤的表單。表單自身的 state(kind/slug/auto/busy)封在這裡;
// 成功後透過 onAdded(=page 的 load)刷新清單、錯誤透過 onError 顯示在
// page 層的共用錯誤框(維持原本的資料流)。
export default function AddTrackedForm({
  onAdded,
  onError,
}: {
  onAdded: () => void;
  onError: (message: string | null) => void;
}) {
  const [addKind, setAddKind] = useState<TrackedKind>("studio");
  const [addSlug, setAddSlug] = useState("");
  const [addAuto, setAddAuto] = useState(false);
  const [addBusy, setAddBusy] = useState(false);

  async function manualAdd(e: React.FormEvent) {
    e.preventDefault();
    // Be forgiving: strip leading kind/ and surrounding slashes so the
    // user can paste either "ca" or "studio/ca" or "/studio/ca/" and
    // we end up with just "ca".
    let slug = addSlug.trim().replace(/^\/+|\/+$/g, "");
    if (slug.toLowerCase().startsWith(`${addKind.toLowerCase()}/`)) {
      slug = slug.slice(addKind.length + 1);
    }
    slug = slug.replace(/^\/+|\/+$/g, "");
    if (!slug) return;
    setAddBusy(true);
    onError(null);
    try {
      // name="" → backend tries to fetch the listing's real title.
      await api.post<TrackedListing>("/api/tracked", {
        kind: addKind,
        id: slug,
        name: "",
        avatar: "",
        uncensored: false,
        auto_send: addAuto,
      });
      setAddSlug("");
      setAddAuto(false);
      onAdded();
    } catch (e: any) {
      onError(e.message);
    } finally {
      setAddBusy(false);
    }
  }

  return (
    <form
      onSubmit={manualAdd}
      className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card/50 px-3 py-2 text-sm"
    >
      <span className="text-xs text-muted-foreground">手動新增</span>
      <select
        value={addKind}
        onChange={(e) => setAddKind(e.target.value as TrackedKind)}
        className="rounded-md border border-border bg-background px-2 py-1 text-xs"
      >
        {ADD_KINDS.map((k) => (
          <option key={k.value} value={k.value}>
            {k.label}
          </option>
        ))}
      </select>
      <Input
        value={addSlug}
        onChange={(e) => setAddSlug(e.target.value)}
        placeholder="JavBus slug, 例如 studio/ca 的 ca"
        className="h-8 min-w-[180px] flex-1 px-2 font-mono text-xs md:text-xs"
      />
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Checkbox
          checked={addAuto}
          onCheckedChange={(v) => setAddAuto(v === true)}
        />
        自動送 PikPak
      </label>
      <Button type="submit" size="sm" disabled={addBusy || !addSlug.trim()}>
        {addBusy ? "新增中…" : "+ 追蹤"}
      </Button>
    </form>
  );
}
