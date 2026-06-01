import type { Metadata } from "next";
import AuthGate from "@/components/AuthGate";
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
          <AuthGate>{children}</AuthGate>
        </ToastProvider>
      </body>
    </html>
  );
}
