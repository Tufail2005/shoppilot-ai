import Link from "next/link";

function LogoMark() {
  return (
    <div className="relative flex size-9 items-center justify-center transition-transform hover:scale-105">
      <div className="absolute size-7 rotate-45 rounded-[8px] bg-[#111] shadow-sm" />
      <div className="absolute size-3 rounded-[3px] bg-[#f7f5f0]" />
    </div>
  );
}

export function Footer({ apiStatus }: { apiStatus: string }) {
  const online = apiStatus === "ok";

  return (
    <footer className="bg-[#f7f5f0] border-t border-black/10">
      <div className="mx-auto flex max-w-[1536px] flex-col gap-8 px-5 py-5 sm:px-8 lg:flex-row lg:items-center lg:justify-between lg:px-14 xl:px-16">
        
        {/* =========================================================
            LEFT: BRANDING
           ========================================================= */}
        <div className="flex items-center gap-4">
          <LogoMark />

          <div>
            <p className="text-[17px] font-bold tracking-tight text-[#111]">
              ShopPilot
            </p>
            <p className="mt-0.5 text-[14px] font-medium text-gray-500">
              Smarter support for modern commerce.
            </p>
          </div>
        </div>

        {/* =========================================================
            RIGHT: LINKS, SOCIALS, & STATUS
           ========================================================= */}
        <div className="flex flex-wrap items-center gap-x-8 gap-y-6">
          
          {/* Navigation Links */}
          <div className="flex items-center gap-6 text-[14px] font-medium text-gray-500">
            <Link href="/login" className="transition-colors hover:text-[#111]">
              Log in
            </Link>
            <Link href="/signup" className="transition-colors hover:text-[#111]">
              Get started
            </Link>
            <a href="#product" className="transition-colors hover:text-[#111]">
              Product
            </a>
            <a href="#workflow" className="transition-colors hover:text-[#111]">
              How it works
            </a>
          </div>

          {/* Social Icons (Using Inline SVGs to avoid missing Lucide exports) */}
          <div className="flex items-center gap-5 border-l border-black/10 pl-8">
            {/* X / Twitter */}
            <a 
              href="https://twitter.com" 
              target="_blank" 
              rel="noreferrer"
              className="text-gray-400 transition-all duration-200 hover:text-[#111] hover:-translate-y-0.5"
            >
              <svg viewBox="0 0 24 24" className="h-[18px] w-[18px]" fill="currentColor">
                <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
              </svg>
            </a>
            
            {/* GitHub */}
            <a 
              href="https://github.com" 
              target="_blank" 
              rel="noreferrer"
              className="text-gray-400 transition-all duration-200 hover:text-[#111] hover:-translate-y-0.5"
            >
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor">
                <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" />
              </svg>
            </a>
            
            {/* LinkedIn */}
            <a 
              href="https://linkedin.com" 
              target="_blank" 
              rel="noreferrer"
              className="text-gray-400 transition-all duration-200 hover:text-[#111] hover:-translate-y-0.5"
            >
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor">
                <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" />
              </svg>
            </a>
          </div>

          {/* API Status */}
          <div className="flex items-center gap-2.5 border-l border-black/10 pl-8 text-[13px] font-medium text-gray-600">
            <span className="relative flex h-2.5 w-2.5">
              {online && (
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75"></span>
              )}
              <span 
                className={`relative inline-flex size-2.5 rounded-full ${
                  online ? "bg-emerald-500" : "bg-red-500"
                }`}
              ></span>
            </span>

            <span>
              {online ? "API operational" : "API offline"}
            </span>
          </div>
          
        </div>
      </div>
    </footer>
  );
}