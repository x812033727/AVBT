"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { SetMsg } from "./types";

export default function ChangePasswordSection({ setMsg }: { setMsg: SetMsg }) {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!oldPassword || !newPassword) {
      setMsg({ kind: "err", text: "請輸入舊密碼與新密碼" });
      return;
    }
    if (newPassword.length < 6) {
      setMsg({ kind: "err", text: "新密碼至少 6 個字元" });
      return;
    }
    if (newPassword !== confirm) {
      setMsg({ kind: "err", text: "兩次輸入的新密碼不一致" });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.post("/api/auth/change-password", {
        old_password: oldPassword,
        new_password: newPassword,
      });
      setMsg({ kind: "ok", text: "密碼已更新" });
      setOldPassword("");
      setNewPassword("");
      setConfirm("");
    } catch (e: any) {
      setMsg({ kind: "err", text: `修改失敗：${e.message}` });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h2 className="text-lg font-semibold">登入密碼</h2>
      <p className="text-xs text-muted-foreground/80">
        修改本站登入帳號的密碼。修改後既有登入仍有效,直到 token 過期。
      </p>
      <div className="grid gap-2 sm:grid-cols-3">
        <Input
          type="password"
          placeholder="舊密碼"
          value={oldPassword}
          onChange={(e) => setOldPassword(e.target.value)}
        />
        <Input
          type="password"
          placeholder="新密碼（至少 6 字元）"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
        />
        <Input
          type="password"
          placeholder="再次輸入新密碼"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </div>
      <Button onClick={submit} disabled={busy}>
        {busy ? "更新中…" : "更新密碼"}
      </Button>
    </section>
  );
}
