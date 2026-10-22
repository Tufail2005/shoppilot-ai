import { Navbar } from "@/app/components/home/navbar";

const PAGE_SIZE = 5;

export default function ProductsLoading() {
  return (
    <main className="min-h-screen bg-[#f7f5f0] text-[#171717]">
      <Navbar />

      <div className="mx-auto max-w-[1440px] px-5 py-10 sm:px-8 lg:px-12">
        <h1 className="text-2xl font-semibold tracking-tight">Products</h1>
        <p className="mt-2 h-4 w-40 animate-pulse rounded bg-black/10" />

        <div className="mt-6 flex flex-wrap gap-2">
          <span className="h-8 w-14 animate-pulse rounded-full bg-black/10" />
          <span className="h-8 w-20 animate-pulse rounded-full bg-black/10" />
          <span className="h-8 w-24 animate-pulse rounded-full bg-black/10" />
        </div>

        <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: PAGE_SIZE }).map((_, index) => (
            <div
              key={index}
              className="h-40 animate-pulse rounded-xl border border-black/10 bg-white"
            />
          ))}
        </div>
      </div>
    </main>
  );
}
