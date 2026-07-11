// 全站統一的狀態語意色階:各頁的狀態 map(PikPak phase、pCloud transfer、
// 下載佇列……)一律先映射到 StatusTone,再交給 <StatusBadge> 渲染。
// 語意約定:pending=neutral、running=info(藍)、done=success、
// failed=danger、cancelled=muted。

export type StatusTone =
  | "neutral"
  | "info"
  | "success"
  | "warning"
  | "danger"
  | "muted";

export type StatusView = { tone: StatusTone; label: string };

/** PikPak 離線任務 phase(PHASE_TYPE_*)→ 統一語意。 */
export function pikpakPhaseTone(phase: string | null | undefined): StatusView {
  switch (phase) {
    case "PHASE_TYPE_COMPLETE":
      return { tone: "success", label: "完成" };
    case "PHASE_TYPE_ERROR":
      return { tone: "danger", label: "失敗" };
    case "PHASE_TYPE_RUNNING":
      return { tone: "info", label: "進行中" };
    case "PHASE_TYPE_PENDING":
      return { tone: "neutral", label: "等待中" };
    default:
      return { tone: "neutral", label: phase || "未知" };
  }
}

/** pCloud 轉存佇列 status → 統一語意。 */
export function transferStatusTone(status: string | null | undefined): StatusView {
  switch (status) {
    case "done":
      return { tone: "success", label: "完成" };
    case "failed":
      return { tone: "danger", label: "失敗" };
    case "running":
      return { tone: "info", label: "傳輸中" };
    case "cancelled":
      return { tone: "muted", label: "已取消" };
    case "pending":
      return { tone: "neutral", label: "等待中" };
    default:
      return { tone: "neutral", label: status || "未知" };
  }
}
