import Link from "next/link";
import { Check } from "lucide-react";
import { imgProxy, type CachedDetailLite, type MovieListItem } from "@/lib/api";
import { cn } from "@/lib/utils";

export default function MovieCard({
  item,
  present,
  meta,
  selectable,
  selected,
  onToggleSelect,
}: {
  item: MovieListItem;
  /** Cache-join metadata line (studio/series/genres); absent = no line. */
  meta?: CachedDetailLite;
  /** true = exists in PikPak, false = missing, undefined = unknown */
  present?: boolean;
  /** Multi-select mode: show a checkbox overlay instead of navigating. */
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (code: string, on: boolean) => void;
}) {
  const body = (
    <>
      <div className="aspect-[5/7] w-full overflow-hidden bg-black">
        {item.cover && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={imgProxy(item.cover)}
            alt={item.title}
            loading="lazy"
            referrerPolicy="no-referrer"
            className="h-full w-full object-cover object-top transition group-hover:scale-[1.02]"
          />
        )}
        {selectable && (
          <span
            className={cn(
              "absolute left-2 top-2 flex h-5 w-5 items-center justify-center rounded-sm border",
              selected
                ? "border-primary bg-primary text-primary-foreground"
                : "border-white/50 bg-black/50",
            )}
            aria-hidden
          >
            {selected && <Check className="h-3.5 w-3.5" />}
          </span>
        )}
        {present === true && (
          <span className="absolute right-2 top-2 inline-flex items-center gap-0.5 rounded-sm bg-emerald-500/85 px-1.5 py-0.5 text-[10px] font-semibold text-black">
            <Check className="h-3 w-3" aria-hidden />
            已下載
          </span>
        )}
        {present === false && (
          <span className="absolute right-2 top-2 rounded-sm bg-amber-400/90 px-1.5 py-0.5 text-[10px] font-semibold text-black">
            缺漏
          </span>
        )}
      </div>
      <div className="px-3 py-2">
        <div className="text-sm font-semibold text-primary">{item.code}</div>
        <div className="line-clamp-2 text-sm text-foreground/80">{item.title}</div>
        {meta && (meta.studio || meta.series || meta.genres.length > 0) && (
          <div className="mt-0.5 truncate text-xs text-muted-foreground/70">
            {[
              meta.studio?.name,
              meta.series?.name,
              ...meta.genres.slice(0, 2),
            ]
              .filter(Boolean)
              .join("・")}
          </div>
        )}
        {item.date && (
          <div className="mt-1 text-xs text-muted-foreground">{item.date}</div>
        )}
      </div>
    </>
  );

  const className = cn(
    "group relative block overflow-hidden rounded-lg border bg-card transition",
    selectable && selected
      ? "border-primary ring-1 ring-primary"
      : present === false
      ? "border-amber-400/40 hover:border-amber-300"
      : present === true
      ? "border-emerald-500/30 hover:border-emerald-400/70"
      : "border-border hover:border-primary/50",
  );

  if (selectable) {
    return (
      <button
        type="button"
        onClick={() => onToggleSelect?.(item.code, !selected)}
        className={cn(className, "w-full text-left")}
      >
        {body}
      </button>
    );
  }

  return (
    <Link href={`/movie/${encodeURIComponent(item.code)}`} className={className}>
      {body}
    </Link>
  );
}
