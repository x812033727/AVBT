// Shared helpers for the settings page sections.
// 格式化實作已上移至 lib/format.ts;此檔僅保留 settings 專用的包裝與型別,
// 待 settings 頁遷移完成後移除。

import { fmtBytes as baseFmtBytes, fmtDateTime } from "@/lib/format";

export type SetMsg = (m: { kind: "ok" | "err"; text: string } | null) => void;

export const fmtBytes = baseFmtBytes;

export function fmt(d: string | null): string {
  return fmtDateTime(d, "從未執行");
}
