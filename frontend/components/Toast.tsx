"use client";

// 全站通知/確認的統一入口。
// 公開 API(toast.* / confirmDialog / useToast / default ToastProvider)維持
// 原樣,內部實作換為 sonner(toast 疊層)+ Radix AlertDialog(confirm,含
// focus trap / Esc / role 語意)。confirm 維持佇列語意:多筆依序呈現。

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { toast as sonnerToast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

export type ToastKind = "info" | "success" | "warn" | "error";

type ConfirmRequest = {
  id: number;
  message: string;
  detail?: string;
  resolve: (ok: boolean) => void;
};

type ConfirmListener = (req: Omit<ConfirmRequest, "id">) => void;

const confirmListeners = new Set<ConfirmListener>();

export const toast = {
  info: (message: string) => void sonnerToast.info(message, { duration: 3000 }),
  success: (message: string) =>
    void sonnerToast.success(message, { duration: 3000 }),
  warn: (message: string) =>
    void sonnerToast.warning(message, { duration: 3000 }),
  error: (message: string) =>
    void sonnerToast.error(message, { duration: 5000 }),
};

export function confirmDialog(message: string, detail?: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    if (confirmListeners.size === 0) {
      // Provider 尚未掛載(理論上不會發生):不讓呼叫端永遠卡住。
      resolve(false);
      return;
    }
    confirmListeners.forEach((l) => l({ message, detail, resolve }));
  });
}

const ToastContext = createContext<typeof toast>(toast);
export function useToast() {
  return useContext(ToastContext);
}

export default function ToastProvider({ children }: { children: React.ReactNode }) {
  const [confirms, setConfirms] = useState<ConfirmRequest[]>([]);
  const counter = useRef(0);

  useEffect(() => {
    const handler: ConfirmListener = (req) => {
      const id = ++counter.current;
      setConfirms((prev) => [...prev, { id, ...req }]);
    };
    confirmListeners.add(handler);
    return () => {
      confirmListeners.delete(handler);
    };
  }, []);

  const settleConfirm = useCallback((id: number, ok: boolean) => {
    setConfirms((prev) => {
      const target = prev.find((c) => c.id === id);
      if (target) target.resolve(ok);
      return prev.filter((c) => c.id !== id);
    });
  }, []);

  const top = confirms[0];

  const ctx = useMemo(() => toast, []);

  return (
    <ToastContext.Provider value={ctx}>
      {children}
      <Toaster position="bottom-right" closeButton />
      <AlertDialog
        open={!!top}
        onOpenChange={(open) => {
          // Esc / 點遮罩關閉視為取消;按鈕路徑已先 settle,這裡的重複呼叫
          // 會因佇列中找不到該 id 而自然變成 no-op。
          if (!open && top) settleConfirm(top.id, false);
        }}
      >
        {top && (
          <AlertDialogContent className="max-w-sm">
            <AlertDialogHeader>
              <AlertDialogTitle className="text-sm font-normal text-foreground">
                {top.message}
              </AlertDialogTitle>
              {top.detail ? (
                <AlertDialogDescription className="text-xs">
                  {top.detail}
                </AlertDialogDescription>
              ) : null}
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel onClick={() => settleConfirm(top.id, false)}>
                取消
              </AlertDialogCancel>
              <AlertDialogAction onClick={() => settleConfirm(top.id, true)}>
                確認
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        )}
      </AlertDialog>
    </ToastContext.Provider>
  );
}
