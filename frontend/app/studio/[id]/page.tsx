import ListingPage from "@/components/ListingPage";

export default function StudioPage({ params }: { params: { id: string } }) {
  return (
    <ListingPage kind="studio" id={decodeURIComponent(params.id)} label="製作商" />
  );
}
