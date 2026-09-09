import { getApiHealth } from "@/lib/api";

export default async function Home() {
  let apiStatus = "offline";

  try {
    const health = await getApiHealth();
    apiStatus = health.status;
  } catch {
    apiStatus = "offline";
  }

  return (
    <main className="flex min-h-screen items-center justify-center">
      <div className="space-y-4 text-center">
        <h1 className="text-3xl font-semibold">
          ShopPilot AI
        </h1>

        <p>
          API:{" "}
          <span className="font-mono">
            {apiStatus}
          </span>
        </p>
      </div>
    </main>
  );
}