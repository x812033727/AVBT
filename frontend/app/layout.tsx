import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import ToastProvider from "@/components/Toast";
import "./globals.css";

export const metadata: Metadata = {
  title: "AVBT",
  description: "JavBus magnet manager + PikPak offline",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-Hant">
      <body className="min-h-screen bg-ink text-white/90">
        <ToastProvider>
          <Navbar />
          <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
        </ToastProvider>
      </body>
    </html>
  );
}
