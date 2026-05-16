import ListingPage from "@/components/ListingPage";

export default function SeriesPage({ params }: { params: { id: string } }) {
  return (
    <ListingPage kind="series" id={decodeURIComponent(params.id)} label="系列" />
  );
}
