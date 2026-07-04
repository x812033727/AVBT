"use client";

import { useState } from "react";
import type { TrendPoint } from "@/lib/api";

/**
 * Zero-dependency 30-day activity chart: one column per day, sent as the
 * full bar, archived as the brighter overlay. Pure SVG so we don't pull
 * in a chart library for a single view.
 */
export default function TrendBars({ points }: { points: TrendPoint[] }) {
  const [hover, setHover] = useState<TrendPoint | null>(null);
  const max = Math.max(1, ...points.map((p) => p.sent));
  const W = 600;
  const H = 120;
  const gap = 2;
  const bw = (W - gap * (points.length - 1)) / Math.max(1, points.length);

  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-28 w-full"
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="近 30 天送出與歸檔數量"
      >
        {points.map((p, i) => {
          const x = i * (bw + gap);
          const sentH = (p.sent / max) * (H - 4);
          const archH = (p.archived / max) * (H - 4);
          return (
            <g key={p.date} onMouseEnter={() => setHover(p)}>
              {/* hit area covers the full column height */}
              <rect x={x} y={0} width={bw} height={H} fill="transparent" />
              <rect
                x={x}
                y={H - sentH}
                width={bw}
                height={sentH}
                className="fill-white/20"
                rx={1}
              />
              <rect
                x={x}
                y={H - archH}
                width={bw}
                height={archH}
                className="fill-accent/70"
                rx={1}
              />
            </g>
          );
        })}
      </svg>
      <div className="mt-1 flex items-center justify-between text-xs text-white/40">
        <span>{points[0]?.date ?? ""}</span>
        <span>
          {hover
            ? `${hover.date}:送出 ${hover.sent} ・ 歸檔 ${hover.archived}`
            : "灰=送出 ・ 亮=歸檔"}
        </span>
        <span>{points[points.length - 1]?.date ?? ""}</span>
      </div>
    </div>
  );
}
