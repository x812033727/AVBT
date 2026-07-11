"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, setToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBox } from "@/components/shared/ErrorBox";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);

  // Fresh install with no account yet → send the user to first-run setup.
  useEffect(() => {
    let alive = true;
    api
      .get<{ needs_setup: boolean }>("/api/auth/status")
      .then((s) => {
        if (!alive) return;
        if (s.needs_setup) router.replace("/setup");
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
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<{ token: string; username: string }>(
        "/api/auth/login",
        { username, password }
      );
      setToken(res.token);
      router.replace("/");
    } catch (err: any) {
      setError(err.message || "登入失敗");
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
          <p className="mt-1 text-sm text-muted-foreground">請登入以繼續</p>
        </div>
        <form onSubmit={submit} className="space-y-3">
          <Input
            autoFocus
            type="text"
            placeholder="帳號"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
          <Input
            type="password"
            placeholder="密碼"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {error && <ErrorBox message={error} />}
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "登入中…" : "登入"}
          </Button>
        </form>
        <p className="text-center text-xs text-muted-foreground/60">
          忘記密碼?在伺服器上執行{" "}
          <code className="rounded bg-muted px-1">
            touch backend/data/reset_password
          </code>{" "}
          後重啟即可重新設定
        </p>
      </div>
    </div>
  );
}
