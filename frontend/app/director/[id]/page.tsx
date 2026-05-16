import ListingPage from "@/components/ListingPage";

export default function DirectorPage({ params }: { params: { id: string } }) {
  return (
    <ListingPage kind="director" id={decodeURIComponent(params.id)} label="導演" />
  );
}
