"use client";

import { useState } from "react";
import { toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, type PCloudStatus } from "@/lib/api";

// 登入面板(從 app/pcloud/page.tsx 拆出,props 照原樣)。
export default function LoginPanel({
  status,
  onChanged,
}: {
  status: PCloudStatus | null;
  onChanged: () => void;
}) {
  const [mode, setMode] = useState<"password" | "token">("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Sticky in-form error: toasts auto-dismiss but the multi-line
  // diagnostic message from /api/pcloud/login needs to stay visible long
  // enough for the user to read all three possible causes.
  const [loginError, setLoginError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setLoginError(null);
    try {
      const body =
        mode === "token"
          ? { access_token: token.trim() }
          : { username: username.trim(), password };
      await api.post("/api/pcloud/login", body);
      toast.success("pCloud 登入成功");
      setUsername("");
      setPassword("");
      setToken("");
      onChanged();
    } catch (e: any) {
      const msg = e?.message || "登入失敗";
      setLoginError(msg);
      toast.error(msg.split("\n")[0]);
    } finally {
      setSubmitting(false);
    }
  }

  if (!status) return null;

  return (
    <div className="space-y-3 rounded-md border border-border bg-card px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span
          className="h-2 w-2 shrink-0 rounded-full bg-amber-400"
          aria-hidden
        />
        <span className="text-foreground/90">尚未登入 pCloud</span>
        {(status.has_env_credentials || status.has_env_token) && (
          <span className="text-xs text-muted-foreground/70">
            (.env 已設定 — 首次呼叫 API 會自動登入)
          </span>
        )}
      </div>
      <form onSubmit={submit} className="space-y-2">
        <div className="flex gap-1">
          <Button
            type="button"
            size="sm"
            variant={mode === "password" ? "default" : "ghost"}
            onClick={() => setMode("password")}
          >
            帳密
          </Button>
          <Button
            type="button"
            size="sm"
            variant={mode === "token" ? "default" : "ghost"}
            onClick={() => setMode("token")}
          >
            Access Token
          </Button>
        </div>
        {mode === "password" ? (
          <div className="flex flex-wrap gap-2">
            <Input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="pCloud Email"
              className="h-8 flex-1"
              autoComplete="username"
            />
            <Input
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="密碼"
              type="password"
              className="h-8 flex-1"
              autoComplete="current-password"
            />
          </div>
        ) : (
          <Input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="貼上 pCloud access token"
            className="h-8 w-full font-mono"
          />
        )}
        <Button type="submit" size="sm" disabled={submitting}>
          {submitting ? "登入中…" : "登入"}
        </Button>
        {loginError && (
          <div
            role="alert"
            className="whitespace-pre-wrap rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs leading-relaxed text-red-300"
          >
            {loginError}
          </div>
        )}
      </form>
    </div>
  );
}
