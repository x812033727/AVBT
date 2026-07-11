import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

// 影片卡片網格:取代原先重複 5 處的 grid class(ListingPage/search/missing/star/Skeleton)。
export function MovieGrid({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export function MovieGridSkeleton({ count = 10 }: { count?: number }) {
  return (
    <MovieGrid aria-hidden>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="overflow-hidden rounded-lg border border-border bg-card">
          <Skeleton className="aspect-[5/7] w-full rounded-none" />
          <div className="space-y-2 p-2">
            <Skeleton className="h-3 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        </div>
      ))}
    </MovieGrid>
  );
}
