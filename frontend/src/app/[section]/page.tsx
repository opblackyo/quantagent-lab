import { Suspense } from "react";
import { notFound } from "next/navigation";
import Workstation from "@/components/workstation";
export default async function Section({
  params,
}: {
  params: Promise<{ section: string }>;
}) {
  const { section } = await params;
  if (!["market", "agents", "backtests", "runs", "settings", "research"].includes(section))
    notFound();
  return (
    <Suspense fallback={<p>載入研究工作台…</p>}>
      <Workstation />
    </Suspense>
  );
}
