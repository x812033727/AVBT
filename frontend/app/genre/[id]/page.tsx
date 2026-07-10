import ListingPage from "@/components/ListingPage";

export default function GenrePage({ params }: { params: { id: string } }) {
  return (
    <ListingPage kind="genre" id={decodeURIComponent(params.id)} label="類別" />
  );
}
