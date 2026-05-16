import Link from "next/link";
import { imgProxy, type MovieListItem } from "@/lib/api";

export default function MovieCard({
  item,
  present,
}: {
  item: MovieListItem;
  /** true = exists in PikPak, false = missing, undefined = unknown */
  present?: boolean;
}) {
  return (
    <Link
      href={`/movie/${encodeURIComponent(item.code)}`}
      className={
        "group relative block overflow-hidden rounded-lg border bg-panel transition " +
        (present === false
          ? "border-amber-400/40 hover:border-amber-300"
          : present === true
          ? "border-emerald-500/30 hover:border-emerald-400/70"
          : "border-white/10 hover:border-accent/60")
      }
    >
      <div className="aspect-[16/11] w-full overflow-hidden bg-black">
        {item.cover && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={imgProxy(item.cover)}
            alt={item.title}
            loading="lazy"
            referrerPolicy="no-referrer"
            className="h-full w-full object-cover object-right transition group-hover:scale-[1.02]"
          />
        )}
        {present === true && (
          <span className="absolute right-2 top-2 rounded bg-emerald-500/85 px-1.5 py-0.5 text-[10px] font-semibold text-black">
            ✓ 已下載
          </span>
        )}
        {present === false && (
          <span className="absolute right-2 top-2 rounded bg-amber-400/90 px-1.5 py-0.5 text-[10px] font-semibold text-black">
            缺漏
          </span>
        )}
      </div>
      <div className="px-3 py-2">
        <div className="text-sm font-semibold text-accent">{item.code}</div>
        <div className="line-clamp-2 text-sm text-white/80">{item.title}</div>
        {item.date && (
          <div className="mt-1 text-xs text-white/40">{item.date}</div>
        )}
      </div>
    </Link>
  );
}
