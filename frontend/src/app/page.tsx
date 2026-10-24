import { Suspense } from "react";
import Workstation from "@/components/workstation";
export default function Home() {
  return (
    <Suspense fallback={<p>載入研究工作台…</p>}>
      <Workstation />
    </Suspense>
  );
}
