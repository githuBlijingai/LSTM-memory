import { Suspense } from "react";
import { SearchView } from "@/features/search/SearchView";
import { LoadingState } from "@/components/states/StatePanels";

export default function SearchPage() {
  return (
    <Suspense fallback={<LoadingState label="正在准备搜索页面…" />}>
      <SearchView />
    </Suspense>
  );
}
