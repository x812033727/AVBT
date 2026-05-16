import Link from "next/link";
import { imgProxy, type MovieListItem } from "@/lib/api";

export default function MovieCard({ item }: { item: MovieListItem }) {
  return (
    <Link
      href={`/movie/${encodeURIComponent(item.code)}`}
      className="group block overflow-hidden rounded-lg border border-white/10 bg-panel transition hover:border-accent/60"
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
