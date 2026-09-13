import Link from "next/link";

function LogoMark() {
  return (
    <div className="relative flex size-9 shrink-0 items-center justify-center">
      <div className="absolute size-7 rotate-45 rounded-[7px] bg-[#171717]" />
      <div className="absolute size-3.5 rounded-[3px] bg-[#f7f5f0]" />
      <div className="absolute left-[7px] top-[7px] size-2 rounded-full bg-[#171717]" />
    </div>
  );
}

export function Navbar() {
  return (
    <header className="border-b border-black/10 bg-[#f7f5f0]">
      <div className="mx-auto flex h-[76px] max-w-[1440px] items-center justify-between px-5 sm:px-8 lg:px-12">
        <Link href="/" className="flex items-center gap-2.5">
          <LogoMark />

          <span className="text-[18px] font-semibold tracking-[-0.03em]">
            ShopPilot
          </span>
        </Link>

        <nav className="hidden items-center gap-8 md:flex">
          <a
            href="#product"
            className="text-sm text-black/60 transition hover:text-black"
          >
            Product
          </a>

          <a
            href="#workflow"
            className="text-sm text-black/60 transition hover:text-black"
          >
            How it works
          </a>

          <a
            href="#architecture"
            className="text-sm text-black/60 transition hover:text-black"
          >
            Architecture
          </a>
        </nav>

        <div className="flex items-center gap-2 sm:gap-3">
          <Link
            href="/login"
            className="rounded-lg px-3.5 py-2.5 text-sm font-medium text-black/70 transition hover:bg-black/5 hover:text-black"
          >
            Log in
          </Link>

          <Link
            href="/signup"
            className="inline-flex items-center gap-2 rounded-lg bg-[#171717] px-4 py-2.5 text-sm font-medium text-white transition hover:bg-black/80"
          >
            Get started
            <span>→</span>
          </Link>
        </div>
      </div>
    </header>
  );
}