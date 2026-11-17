import { Suspense } from "react";
import { LoadingState } from "@/components/states/StatePanels";
import { WikiArticleView } from "@/features/wiki/WikiArticleView";

export default function WikiArticlePage() {
  return (
    <Suspense fallback={<LoadingState label="正在准备词条页面…" />}>
      <WikiArticleView />
    </Suspense>
  );
}
