"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  BarChart3,
  Cloud,
  CloudDownload,
  Copy,
  History,
  Home,
  LogOut,
  Menu,
  PackageSearch,
  Radar,
  Search,
  Settings,
  Star,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { clearToken } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";

const TABS: { href: string; label: string; icon: LucideIcon }[] = [
  { href: "/", label: "首頁", icon: Home },
  { href: "/dashboard", label: "統計", icon: BarChart3 },
  { href: "/search", label: "搜尋", icon: Search },
  { href: "/collection", label: "收藏", icon: Star },
  { href: "/tracked", label: "追蹤", icon: Radar },
  { href: "/actresses", label: "女優", icon: Users },
  { href: "/missing", label: "缺漏", icon: PackageSearch },
  { href: "/pikpak", label: "PikPak", icon: CloudDownload },
  { href: "/pcloud", label: "pCloud", icon: Cloud },
  { href: "/duplicates", label: "重複", icon: Copy },
  { href: "/history", label: "紀錄", icon: History },
  { href: "/settings", label: "設定", icon: Settings },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export default function Navbar() {
  const pathname = usePathname();
  const router = useRouter();
  const [isMac, setIsMac] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    setIsMac(/Mac|iPhone|iPad/.test(navigator.platform));
  }, []);

  // 換頁時收合手機選單。
  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

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

  function logout() {
    clearToken();
    router.push("/login");
  }

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/80 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-4 px-4">
        {/* 手機:漢堡選單 */}
        <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
          <SheetTrigger asChild>
            <button
              type="button"
              className="inline-flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground lg:hidden"
              aria-label="開啟選單"
            >
              <Menu className="h-5 w-5" aria-hidden />
            </button>
          </SheetTrigger>
          <SheetContent side="left" className="flex w-64 flex-col gap-0 border-border bg-background p-0">
            <SheetHeader className="border-b border-border px-4 py-3 text-left">
              <SheetTitle className="text-base font-bold tracking-wide text-primary">
                AVBT
              </SheetTitle>
            </SheetHeader>
            <nav className="flex flex-col gap-0.5 overflow-y-auto p-2" aria-label="主選單">
              {TABS.map((t) => {
                const active = isActive(pathname, t.href);
                const Icon = t.icon;
                return (
                  <Link
                    key={t.href}
                    href={t.href}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition",
                      active
                        ? "bg-muted text-foreground"
                        : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                    )}
                  >
                    <Icon
                      className={cn("h-4 w-4", active && "text-primary")}
                      aria-hidden
                    />
                    {t.label}
                  </Link>
                );
              })}
            </nav>
            <div className="mt-auto border-t border-border p-2">
              <button
                type="button"
                onClick={logout}
                className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted/60 hover:text-foreground"
              >
                <LogOut className="h-4 w-4" aria-hidden />
                登出
              </button>
            </div>
          </SheetContent>
        </Sheet>

        <Link href="/" className="text-lg font-bold tracking-wide text-primary">
          AVBT
        </Link>

        {/* 桌面:水平導覽,active 以橘色底線標示 */}
        <nav className="hidden h-full items-stretch gap-0.5 lg:flex" aria-label="主選單">
          {TABS.map((t) => {
            const active = isActive(pathname, t.href);
            return (
              <Link
                key={t.href}
                href={t.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative inline-flex items-center rounded-md px-3 text-sm transition",
                  active
                    ? "text-foreground"
                    : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                )}
              >
                {t.label}
                {active && (
                  <span
                    className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-primary"
                    aria-hidden
                  />
                )}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <span
            className="hidden items-center gap-1 rounded-md border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground md:inline-flex"
            title="任何頁面按下後跳到搜尋"
          >
            {isMac ? "⌘" : "Ctrl"}+K
          </span>
          <button
            type="button"
            onClick={logout}
            className="hidden items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground lg:inline-flex"
            title="登出"
          >
            <LogOut className="h-3.5 w-3.5" aria-hidden />
            登出
          </button>
        </div>
      </div>
    </header>
  );
}
