"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

const NOTIFY_EVENTS: { key: string; label: string; hint: string }[] = [
  { key: "tracked_new", label: "追蹤新作", hint: "追蹤的女優/系列出現新作品" },
  { key: "archive_done", label: "歸檔完成", hint: "檔案自動歸檔到分類資料夾" },
  { key: "archive_failed", label: "歸檔失敗", hint: "同一檔案只通知第一次失敗" },
  { key: "download_failed", label: "下載送出失敗", hint: "PikPak 不穩時可能較吵，預設關閉" },
];

type NotifySettings = {
  webhook_configured: boolean;
  telegram_configured: boolean;
  toggles: Record<string, boolean>;
};

export default function NotifySection({
  setMsg,
}: {
  setMsg: (m: { kind: "ok" | "err"; text: string } | null) => void;
}) {
  const [conf, setConf] = useState<NotifySettings | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const c = await api.get<NotifySettings>("/api/notify/settings").catch(() => null);
    setConf(c);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function toggle(event: string, enabled: boolean) {
    if (!conf) return;
    // Optimistic flip; reload on failure.
    setConf({ ...conf, toggles: { ...conf.toggles, [event]: enabled } });
    try {
      await api.post("/api/notify/settings", { toggles: { [event]: enabled } });
    } catch (e: any) {
      setMsg({ kind: "err", text: `儲存通知設定失敗：${e.message}` });
      await load();
    }
  }

  async function sendTest() {
    setBusy(true);
    try {
      const r = await api.post<{
        ok: boolean;
        results: Record<string, boolean>;
        message?: string;
      }>("/api/notify/test");
      if (!Object.keys(r.results).length) {
        setMsg({ kind: "err", text: r.message || "沒有設定任何通知管道" });
      } else {
        const parts = Object.entries(r.results).map(
          ([ch, ok]) => `${ch}: ${ok ? "✓ 成功" : "✗ 失敗"}`
        );
        setMsg({ kind: r.ok ? "ok" : "err", text: `測試通知結果 — ${parts.join(" ・ ")}` });
      }
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-white/10 bg-panel p-4">
      <h2 className="text-lg font-semibold">通知</h2>
      {conf ? (
        <>
          <div className="text-xs text-white/60">
            Webhook：
            {conf.webhook_configured ? (
              <span className="text-emerald-300">已設定</span>
            ) : (
              <span className="text-white/40">未設定（.env WEBHOOK_URL）</span>
            )}
            {" ・ "}Telegram：
            {conf.telegram_configured ? (
              <span className="text-emerald-300">已設定</span>
            ) : (
              <span className="text-white/40">
                未設定（.env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）
              </span>
            )}
          </div>
          <div className="space-y-2">
            {NOTIFY_EVENTS.map((ev) => (
              <label key={ev.key} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={conf.toggles[ev.key] ?? true}
                  onChange={(e) => toggle(ev.key, e.target.checked)}
                />
                {ev.label}
                <span className="text-xs text-white/40">{ev.hint}</span>
              </label>
            ))}
          </div>
          <button
            className="btn-ghost disabled:opacity-50"
            onClick={sendTest}
            disabled={busy || (!conf.webhook_configured && !conf.telegram_configured)}
          >
            {busy ? "發送中…" : "發送測試通知"}
          </button>
        </>
      ) : (
        <div className="text-sm text-white/40">載入中…</div>
      )}
    </section>
  );
}
