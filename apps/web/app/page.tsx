import { Architecture } from "./components/home/architecture";
import { Footer } from "./components/home/footer";
import { Hero } from "./components/home/hero";
import { HowItWorks } from "./components/home/how-it-works";
import { Navbar } from "./components/home/navbar";
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
    <main className="min-h-screen overflow-x-hidden bg-[#f7f5f0] text-[#171717]">
      <Navbar />
      <Hero />
      {/* <Stats /> */}
      <HowItWorks />
      <Architecture />
      <Footer apiStatus={apiStatus} />
    </main>
  );
}