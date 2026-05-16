"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

export type ToastKind = "info" | "success" | "warn" | "error";

type ToastItem = {
  id: number;
  kind: ToastKind;
  message: string;
};

type ConfirmRequest = {
  id: number;
  message: string;
  detail?: string;
  resolve: (ok: boolean) => void;
};

type EmitterEvent =
  | { type: "toast"; kind: ToastKind; message: string }
  | {
      type: "confirm";
      message: string;
      detail?: string;
      resolve: (ok: boolean) => void;
    };

type Listener = (e: EmitterEvent) => void;

const listeners = new Set<Listener>();

function emit(e: EmitterEvent) {
  listeners.forEach((l) => l(e));
}

export const toast = {
  info: (message: string) => emit({ type: "toast", kind: "info", message }),
  success: (message: string) =>
    emit({ type: "toast", kind: "success", message }),
  warn: (message: string) => emit({ type: "toast", kind: "warn", message }),
  error: (message: string) => emit({ type: "toast", kind: "error", message }),
};

export function confirmDialog(message: string, detail?: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    emit({ type: "confirm", message, detail, resolve });
  });
}

const ToastContext = createContext<typeof toast>(toast);
export function useToast() {
  return useContext(ToastContext);
}

const TONE: Record<ToastKind, string> = {
  info: "border-blue-400/30 bg-blue-500/10 text-blue-100",
  success: "border-emerald-400/30 bg-emerald-500/10 text-emerald-100",
  warn: "border-amber-400/30 bg-amber-500/10 text-amber-100",
  error: "border-red-500/30 bg-red-500/10 text-red-100",
};

const ICON: Record<ToastKind, string> = {
  info: "ⓘ",
  success: "✓",
  warn: "⚠",
  error: "✕",
};

export default function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const [confirms, setConfirms] = useState<ConfirmRequest[]>([]);
  const counter = useRef(0);

  const push = useCallback((kind: ToastKind, message: string) => {
    const id = ++counter.current;
    setItems((prev) => [...prev.slice(-4), { id, kind, message }]);
    const ttl = kind === "error" ? 5000 : 3000;
    setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, ttl);
  }, []);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  useEffect(() => {
    const handler: Listener = (e) => {
      if (e.type === "toast") push(e.kind, e.message);
      else if (e.type === "confirm") {
        const id = ++counter.current;
        setConfirms((prev) => [
          ...prev,
          { id, message: e.message, detail: e.detail, resolve: e.resolve },
        ]);
      }
    };
    listeners.add(handler);
    return () => {
      listeners.delete(handler);
    };
  }, [push]);

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
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 max-w-[90vw] flex-col gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            onClick={() => dismiss(t.id)}
            className={
              "pointer-events-auto flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2 text-sm shadow-lg backdrop-blur " +
              TONE[t.kind]
            }
          >
            <span className="text-base leading-none">{ICON[t.kind]}</span>
            <span className="flex-1 break-words">{t.message}</span>
          </div>
        ))}
      </div>
      {top && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-sm rounded-lg border border-white/10 bg-panel p-4 shadow-xl">
            <div className="text-sm text-white/90">{top.message}</div>
            {top.detail && (
              <div className="mt-2 text-xs text-white/50">{top.detail}</div>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button
                className="btn-ghost"
                onClick={() => settleConfirm(top.id, false)}
              >
                取消
              </button>
              <button
                className="btn-primary"
                onClick={() => settleConfirm(top.id, true)}
              >
                確認
              </button>
            </div>
          </div>
        </div>
      )}
    </ToastContext.Provider>
  );
}
