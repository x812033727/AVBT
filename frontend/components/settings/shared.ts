// Shared helpers for the settings page sections.

export type SetMsg = (m: { kind: "ok" | "err"; text: string } | null) => void;

export function fmtBytes(n?: number | null) {
  if (!n) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(2)} ${u[i]}`;
}

export function fmt(d: string | null): string {
  if (!d) return "從未執行";
  return new Date(d.endsWith("Z") ? d : d + "Z").toLocaleString();
}
