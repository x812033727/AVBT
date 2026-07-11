// 過渡層:舊頁面仍從這裡 import Skeleton / MovieGridSkeleton / RowSkeleton,
// 實作已收斂到 components/ui/skeleton 與 components/shared/MovieGrid,
// PR10 收尾時把 caller 改為直接引用後移除本檔。
import { Skeleton as UISkeleton } from "@/components/ui/skeleton";

export { MovieGridSkeleton } from "@/components/shared/MovieGrid";

export function Skeleton(props: React.HTMLAttributes<HTMLDivElement>) {
  return <UISkeleton aria-hidden {...props} />;
}

export function RowSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: count }).map((_, i) => (
        <Skeleton key={i} className="h-12 w-full" />
      ))}
    </div>
  );
}

export default Skeleton;
