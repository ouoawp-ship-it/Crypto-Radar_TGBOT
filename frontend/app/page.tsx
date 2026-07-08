import { HomeDashboard } from "@/components/HomeDashboard";
import { loadHomeDashboardData } from "@/lib/api";

export default async function Page() {
  const initialData = await loadHomeDashboardData();
  return <HomeDashboard initialData={initialData} />;
}
