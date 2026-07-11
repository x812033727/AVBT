// 全站共用格式化工具:合併原先散落 7 處的 fmtBytes 與 6 處的時間格式化。
// 後端時間欄位多為無時區的 UTC ISO 字串,統一補 "Z" 再解析。

export function fmtBytes(n?: number | null, empty = "-"): string {
  if (!n) return empty;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(2)} ${u[i]}`;
}

function toDate(iso: string): Date {
  return new Date(iso.endsWith("Z") ? iso : iso + "Z");
}

export function fmtDateTime(iso: string | null | undefined, empty = "-"): string {
  if (!iso) return empty;
  return toDate(iso).toLocaleString();
}

export function fmtTime(iso: string | null | undefined, empty = "-"): string {
  if (!iso) return empty;
  try {
    return toDate(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

export function fmtRel(iso: string | null | undefined, empty = "-"): string {
  if (!iso) return empty;
  const ms = Date.now() - toDate(iso).getTime();
  if (ms < 60_000) return "剛剛";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)} 分鐘前`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)} 小時前`;
  return `${Math.floor(ms / 86_400_000)} 天前`;
}
