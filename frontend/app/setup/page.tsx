"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, setToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBox } from "@/components/shared/ErrorBox";

export default function SetupPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);

  // Already configured → there's nothing to set up; go log in instead.
  useEffect(() => {
    let alive = true;
    api
      .get<{ needs_setup: boolean }>("/api/auth/status")
      .then((s) => {
        if (!alive) return;
        if (!s.needs_setup) router.replace("/login");
        else setChecking(false);
      })
      .catch(() => alive && setChecking(false));
    return () => {
      alive = false;
    };
  }, [router]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!username || !password) {
      setError("請輸入帳號與密碼");
      return;
    }
    if (password.length < 6) {
      setError("密碼至少 6 個字元");
      return;
    }
    if (password !== confirm) {
      setError("兩次輸入的密碼不一致");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<{ token: string; username: string }>(
        "/api/auth/setup",
        { username, password }
      );
      setToken(res.token);
      router.replace("/");
    } catch (err: any) {
      setError(err.message || "設定失敗");
    } finally {
      setBusy(false);
    }
  }

  if (checking) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        載入中…
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm space-y-5 rounded-xl border border-border bg-card p-6 shadow-xl">
        <div className="text-center">
          <div className="text-2xl font-bold tracking-wide text-primary">AVBT</div>
          <p className="mt-1 text-sm text-muted-foreground">首次使用：設定管理員帳密</p>
        </div>
        <form onSubmit={submit} className="space-y-3">
          <Input
            autoFocus
            type="text"
            placeholder="設定帳號"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
          <Input
            type="password"
            placeholder="設定密碼（至少 6 字元）"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <Input
            type="password"
            placeholder="再次輸入密碼"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
          {error && <ErrorBox message={error} />}
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "設定中…" : "建立帳號並進入"}
          </Button>
        </form>
      </div>
    </div>
  );
}
