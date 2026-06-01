"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import Navbar from "@/components/Navbar";
import { api, getToken } from "@/lib/api";

// Routes that render before the user has a token. They provide their own
// full-screen layout and must NOT be wrapped in the Navbar/main chrome.
const PUBLIC_ROUTES = ["/login", "/setup"];

/**
 * Client-side gate for the single-account login system.
 *
 * - Public routes (/login, /setup) render straight through.
 * - Protected routes: no token → redirect to /login; token present →
 *   validate it once via /api/auth/me, showing a minimal loading state
 *   until it resolves, then render Navbar + page.
 *
 * The token lives in localStorage (client-only), so gating has to happen
 * here rather than in Next.js middleware. Expired/invalid tokens are also
 * caught by the 401 handler in lib/api.ts on the first real request.
 */
export default function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const isPublic = PUBLIC_ROUTES.includes(pathname);
  const [verified, setVerified] = useState(false);

  useEffect(() => {
    if (isPublic) return;
    const token = getToken();
    if (!token) {
      setVerified(false);
      router.replace("/login");
      return;
    }
    if (verified) return; // already validated this session
    let alive = true;
    api
      .get<{ username: string }>("/api/auth/me")
      .then(() => {
        if (alive) setVerified(true);
      })
      .catch(() => {
        // 401 → lib/api already redirected to /login; for other errors
        // (e.g. network) fall back to the login page too.
        if (alive) router.replace("/login");
      });
    return () => {
      alive = false;
    };
  }, [isPublic, pathname, verified, router]);

  if (isPublic) return <>{children}</>;

  if (!verified) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-white/40">
        載入中…
      </div>
    );
  }

  return (
    <>
      <Navbar />
      <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
    </>
  );
}
