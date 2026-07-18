"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, ClipboardList, RefreshCw } from "lucide-react";

import { api, type ArchiverStatus } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

type OpsReport = { header: string; body: string; critical: boolean };
type OpsReports = {
  reports: OpsReport[];
  total: number;
  updated_at: string | null;
};

export default function OpsReportsPage() {
  const [data, setData] = useState<OpsReports | null>(null);
  const [archiver, setArchiver] = useState<ArchiverStatus | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.get<OpsReports>("/api/ops/reports?limit=50"));
      setArchiver(
        await api.get<ArchiverStatus>("/api/pikpak/archiver").catch(() => null)
      );
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 120_000);
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-xl font-bold">
          <ClipboardList className="h-5 w-5" />
          輪值報告
        </h1>
        <div className="flex items-center gap-3">
          {data?.updated_at && (
            <span className="text-xs text-muted-foreground">
              更新於 {new Date(data.updated_at).toLocaleString()}
            </span>
          )}
          <Button size="sm" variant="outline" onClick={() => void load()}>
            <RefreshCw
              className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`}
            />
            重新整理
          </Button>
        </div>
      </div>
      <p className="text-sm text-muted-foreground">
        無人值守輪值每小時驗收自動下載的成果(分集連號、資料夾歸位、垃圾清除),
        並把每輪摘要寫在這裡。標紅的是資料遺失級事件。
      </p>

      {archiver && (
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary" className="font-mono">
            finalize×{archiver.finalize_concurrency}
          </Badge>
          <Badge variant="secondary" className="font-mono">
            poll×{archiver.pcloud_poll_concurrency}
          </Badge>
          {!!archiver.abandoned_total && (
            <Link href="/history?abandoned=true">
              <Badge
                variant="outline"
                className="border-amber-500/40 bg-amber-500/20 font-mono text-amber-300 hover:bg-amber-500/30"
              >
                放棄 {archiver.abandoned_total}
              </Badge>
            </Link>
          )}
        </div>
      )}

      {error && (
        <Card className="border-destructive">
          <CardContent className="py-4 text-sm text-destructive">
            {error}
          </CardContent>
        </Card>
      )}

      {data && data.reports.length === 0 && !error && (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">
            還沒有報告——輪值每小時第 43 分執行。
          </CardContent>
        </Card>
      )}

      {data?.reports.map((r, i) => (
        <Card
          key={`${r.header}-${i}`}
          className={r.critical ? "border-destructive" : undefined}
        >
          <CardHeader className="pb-2">
            <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
              {r.critical && (
                <Badge variant="destructive" className="gap-1">
                  <AlertTriangle className="h-3 w-3" />
                  CRITICAL
                </Badge>
              )}
              <span>{r.header}</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="whitespace-pre-wrap break-words text-xs leading-5 text-muted-foreground">
              {r.body}
            </pre>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
