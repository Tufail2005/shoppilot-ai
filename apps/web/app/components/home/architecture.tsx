import Image from "next/image";

const technologies = [
  {
    id: "nextjs",
    name: "Next.js",
    description: "Modern web experience",
    logo: "https://cdn.jsdelivr.net/gh/devicons/devicon@latest/icons/nextjs/nextjs-original.svg",
  },
  {
    id: "fastapi",
    name: "FastAPI",
    description: "High-performance backend",
    logo: "https://cdn.jsdelivr.net/gh/devicons/devicon@latest/icons/fastapi/fastapi-original.svg",
  },
  {
    id: "postgres",
    name: "PostgreSQL",
    description: "Reliable commerce data",
    logo: "https://cdn.jsdelivr.net/gh/devicons/devicon@latest/icons/postgresql/postgresql-original.svg",
  },
  {
    id: "gemini",
    name: "Gemini + LangGraph",
    description: "Powerful AI agent workflows",
    logo: "https://upload.wikimedia.org/wikipedia/commons/8/8a/Google_Gemini_logo.svg",
  },
];

export function Architecture() {
  return (
    <section id="architecture" className="border-b border-black/10 bg-white">
      {/* 1. Adjusted Grid to shrink the left column slightly */}
      <div className="mx-auto grid max-w-[1536px] lg:grid-cols-[0.85fr_1.15fr]">
        
        {/* =========================================================
            LEFT COLUMN: IMAGE
           ========================================================= */}
        {/* 2. Added padding (p-8 lg:p-16) to frame the image and make it smaller */}
        <div className="flex items-center justify-center p-8 sm:p-12 lg:p-16 lg:border-r border-black/10 bg-[#f7f5f0]">
          
          {/* Framed image container with rounded corners and shadow */}
          <div className="relative w-full aspect-square max-w-[480px] overflow-hidden rounded-3xl border border-black/5 shadow-2xl shadow-black/10 bg-white">
            <Image
              src="/images/laptop-workspace.png"
              alt="Modern workspace"
              fill
              priority // Fixes Next.js LCP warning
              sizes="(max-width: 1024px) 90vw, 40vw" // Fixes Next.js sizes warning
              className="object-cover object-center transition-transform duration-700 hover:scale-105"
            />
          </div>
          
        </div>

        {/* =========================================================
            RIGHT COLUMN: COPY & TECH STACK
           ========================================================= */}
        <div className="flex flex-col justify-center px-5 py-16 sm:px-8 lg:px-14 xl:px-20 lg:py-24 bg-white">
          
          {/* Eyebrow */}
          <div className="mb-6 flex items-center gap-4">
            <span className="h-px w-8 bg-black/20" />
            <span className="rounded-full bg-[#e8e0d4] px-3.5 py-1.5 text-[11px] font-bold uppercase tracking-[0.15em] text-black/70 shadow-sm backdrop-blur-sm">
              Built for scale
            </span>
          </div>

          {/* Heading */}
          <h2 className="text-[36px] sm:text-[44px] font-bold leading-[1.05] tracking-tight text-[#111]">
            Modern infrastructure. <br className="hidden xl:block" />
            <span className="text-[#8C7A6B]">Real business impact.</span>
          </h2>

          {/* Description */}
          <p className="mt-6 max-w-[480px] text-[17px] leading-relaxed text-gray-700 font-medium">
            ShopPilot is built on a modern, production-ready stack to help you
            deliver reliable and secure customer support as your business
            grows.
          </p>

          {/* Technology Cards */}
          <div className="mt-12 flex flex-col gap-4 max-w-[500px]">
            {technologies.map((item) => (
              <div
                key={item.id}
                className="group flex items-center gap-5 rounded-2xl border border-black/5 bg-white p-4 shadow-[0_2px_10px_rgba(0,0,0,0.02)] transition-all duration-300 hover:-translate-y-1 hover:border-black/10 hover:shadow-lg hover:shadow-black/5"
              >
                {/* Logo Box */}
                <div className="flex size-14 shrink-0 items-center justify-center rounded-xl bg-[#F6F4F0] p-3 transition-transform duration-300 group-hover:scale-110">
                  <img 
                    src={item.logo} 
                    alt={`${item.name} logo`} 
                    className="h-full w-full object-contain"
                  />
                </div>

                {/* Text Content */}
                <div>
                  <h3 className="text-[17px] font-bold tracking-tight text-[#111]">
                    {item.name}
                  </h3>
                  <p className="mt-0.5 text-[14px] font-medium text-gray-500">
                    {item.description}
                  </p>
                </div>
              </div>
            ))}
          </div>
          
        </div>
      </div>
    </section>
  );
}