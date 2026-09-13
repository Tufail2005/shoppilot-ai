import { 
  BrainCircuit, 
  Zap, 
  Users, 
  TrendingUp, 
  ArrowRight 
} from "lucide-react";

const steps = [
  {
    number: "01",
    title: "Understand",
    description:
      "AI reads the customer's message and finds the right context from your products, policies, and past orders.",
    icon: BrainCircuit,
  },
  {
    number: "02",
    title: "Take action",
    description:
      "It can check orders, update information, create tickets, or trigger workflows across your tools.",
    icon: Zap,
  },
  {
    number: "03",
    title: "Keep humans in the loop",
    description:
      "Sensitive or complex issues are routed to your team with the full conversation context.",
    icon: Users,
  },
  {
    number: "04",
    title: "Get better over time",
    description:
      "Learn from every conversation to improve answers, workflows, and support operations.",
    icon: TrendingUp,
  },
];

export function HowItWorks() {
  return (
    <section id="workflow" className="border-b border-black/10">
      <div className="mx-auto grid max-w-[1536px] lg:grid-cols-[0.8fr_1.2fr]">
        
        {/* =========================================================
            LEFT COLUMN: INTRO
           ========================================================= */}
        <div className="m-5 border-b border-black/10 px-5 py-16 sm:px-8 lg:border-b-0 lg:border-r lg:px-14 xl:px-16 lg:py-24 flex flex-col justify-center">
          
          {/* Eyebrow matching Hero */}
          <div className="mb-6 flex items-center gap-4">
            <span className="h-px w-8 bg-black/20" />
            <span className="rounded-full bg-[#e8e0d4] px-3.5 py-1.5 text-[11px] font-bold uppercase tracking-[0.15em] text-black/70 shadow-sm backdrop-blur-sm">
              How it works
            </span>
          </div>

          <h2 className="text-[36px] sm:text-[44px] font-bold leading-[1.05] tracking-tight text-[#111]">
            A smarter way to <br className="hidden sm:block" />
            support your <span className="text-[#8C7A6B]">customers.</span>
          </h2>

          <p className="mt-6 max-w-[420px] text-[17px] leading-relaxed text-gray-700 font-medium">
            ShopPilot connects your store, data, and AI agents so your support
            team can handle customer requests end-to-end.
          </p>

          <div className="mt-10">
            {/* Button matching Hero secondary button */}
            <a
              href="#architecture"
              className="inline-flex h-14 items-center justify-center gap-2 rounded-xl border-2 border-black/10 bg-white/50 px-7 text-[15px] font-semibold text-black backdrop-blur-sm transition-all hover:bg-white hover:border-black/20 active:scale-[0.98]"
            >
              Explore the product
              <ArrowRight size={18} className="text-black/70" />
            </a>
          </div>
        </div>

        {/* =========================================================
            RIGHT COLUMN: BOX CARDS
           ========================================================= */}
        <div className="p-5 sm:p-8 lg:p-12 bg-black/[0.02] flex items-center">
          <div className="grid sm:grid-cols-2 gap-5 sm:gap-6 w-full">
            {steps.map((step) => {
              const Icon = step.icon;
              return (
                <article
                  key={step.number}
                  className="group relative flex flex-col bg-white p-7 sm:p-8 rounded-2xl border border-black/5 shadow-sm transition-all duration-300 hover:-translate-y-1 hover:shadow-md hover:border-black/10"
                >
                  <div className="flex items-start justify-between">
                    {/* Icon container with soft beige background to contrast with the white card */}
                    <div className="flex size-14 items-center justify-center rounded-2xl bg-[#F6F4F0] transition-transform duration-300 group-hover:scale-110">
                      <Icon size={24} className="text-[#8C7A6B]" strokeWidth={2.5} />
                    </div>

                    <span className="text-sm font-bold text-black/15 font-mono tracking-wider">
                      {step.number}
                    </span>
                  </div>

                  <h3 className="mt-10 text-[19px] font-bold tracking-tight text-[#111]">
                    {step.title}
                  </h3>

                  <p className="mt-3 text-[14px] leading-relaxed text-gray-600 font-medium">
                    {step.description}
                  </p>
                </article>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}