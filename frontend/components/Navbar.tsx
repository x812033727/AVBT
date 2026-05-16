"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/", label: "首頁" },
  { href: "/search", label: "搜尋" },
  { href: "/collection", label: "收藏" },
  { href: "/tracked", label: "追蹤" },
  { href: "/missing", label: "缺漏" },
  { href: "/pikpak", label: "PikPak" },
  { href: "/history", label: "紀錄" },
  { href: "/settings", label: "設定" },
];

export default function Navbar() {
  const pathname = usePathname();
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
      </div>
    </header>
  );
}
