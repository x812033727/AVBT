"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const TABS = [
  { href: "/", label: "首頁" },
  { href: "/search", label: "搜尋" },
  { href: "/collection", label: "收藏" },
  { href: "/tracked", label: "追蹤" },
  { href: "/missing", label: "缺漏" },
  { href: "/pikpak", label: "PikPak" },
  { href: "/pcloud", label: "pCloud" },
  { href: "/history", label: "紀錄" },
  { href: "/settings", label: "設定" },
];

export default function Navbar() {
  const pathname = usePathname();
  const router = useRouter();
  const [isMac, setIsMac] = useState(false);

  useEffect(() => {
    setIsMac(/Mac|iPhone|iPad/.test(navigator.platform));
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "k") return;
      if (!(e.metaKey || e.ctrlKey)) return;
      e.preventDefault();
      if (pathname === "/search") {
        const el = document.getElementById("search-input") as HTMLInputElement | null;
        if (el) {
          el.focus();
          el.select();
          return;
        }
        window.dispatchEvent(new Event("avbt:focus-search"));
      } else {
        router.push("/search?focus=1");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pathname, router]);

  return (
    <header className="border-b border-white/10 bg-panel/60 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3">
        <Link href="/" className="text-lg font-bold tracking-wide text-accent">
          AVBT
        </Link>
        <nav className="flex gap-1">
          {TABS.map((t) => {
            const active =
              t.href === "/" ? pathname === "/" : pathname.startsWith(t.href);
            return (
              <Link
                key={t.href}
                href={t.href}
                className={
                  "rounded-md px-3 py-1.5 text-sm transition " +
                  (active
                    ? "bg-white/10 text-white"
                    : "text-white/60 hover:bg-white/5 hover:text-white")
                }
              >
                {t.label}
              </Link>
            );
          })}
        </nav>
        <span
          className="ml-auto hidden items-center gap-1 rounded-md border border-white/10 px-2 py-0.5 text-[10px] font-mono text-white/40 md:inline-flex"
          title="任何頁面按下後跳到搜尋"
        >
          {isMac ? "⌘" : "Ctrl"}+K
        </span>
      </div>
    </header>
  );
}
