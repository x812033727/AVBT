import ListingPage from "@/components/ListingPage";

export default function LabelPage({ params }: { params: { id: string } }) {
  return (
    <ListingPage kind="label" id={decodeURIComponent(params.id)} label="發行商" />
  );
}
