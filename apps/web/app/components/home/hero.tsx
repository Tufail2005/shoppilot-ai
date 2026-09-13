import Image from 'next/image';

import {
  CheckCircle2,
  ArrowRight
} from 'lucide-react';

import { DashboardMockup } from './DashboardMock';

const features = [
  "Automate repetitive support",
  "Use your shop data (orders, products, policies)",
  "Keep humans in the loop when it matters",
];


export function Hero() {
return (
    <section className="relative overflow-hidden bg-[#F6F4F0] min-h-screen flex items-center font-sans">
      
      {/* Background Decorators with Plant Image */}
      <div className="absolute inset-0 z-0 pointer-events-none overflow-hidden">
        {/* Plant Background positioned to the right */}
        <img
          src="/images/plant-background.png"
          alt="Plant Background"
          className="absolute right-0 top-0 h-full w-full lg:w-[85%] object-cover object-center lg:object-right"
        />
        
        {/* Gradient fade overlay to blend the image leftward seamlessly into #F6F4F0 */}
        <div 
          className="absolute inset-0" 
          style={{
            background: "linear-gradient(90deg, #F6F4F0 0%, #F6F4F0 35%, rgba(246,244,240,0.85) 55%, rgba(246,244,240,0) 100%)"
          }}
        />
      </div>

      <div className="absolute -bottom-40 -left-40 w-96 h-96 bg-[#F6F4F0] rounded-full blur-3xl opacity-50 z-0 pointer-events-none" />
      
      <div className="container mx-auto px-5 sm:px-8 lg:px-14 xl:px-16 py-20 lg:py-32 relative z-10 max-w-[1536px]">
        <div className="flex flex-col lg:flex-row items-center gap-16 lg:gap-8">
          
          {/* =========================================================
              LEFT COLUMN: HERO COPY
             ========================================================= */}
          <div className="w-full lg:w-[45%] max-w-[620px] shrink-0 pt-10 lg:pt-0">
            {/* Eyebrow */}
            <div className="mb-8 flex items-center gap-4">
              <span className="h-px w-10 bg-black/20" />
              <span className="rounded-full bg-[#e8e0d4] px-3.5 py-1.5 text-[11px] font-bold uppercase tracking-[0.15em] text-black/70 shadow-sm backdrop-blur-sm">
                AI-powered customer support
              </span>
            </div>

            {/* Heading */}
            <h1 className="text-[44px] sm:text-6xl lg:text-[72px] font-bold leading-[1.05] tracking-tight text-[#111] mb-8">
              Turn customer<br className="hidden sm:block" /> conversations into <br className="hidden sm:block" />
              <span className="text-[#8C7A6B]">growth.</span>
            </h1>

            {/* Description */}
            <p className="mb-10 max-w-[500px] text-[17px] leading-relaxed text-gray-700 font-medium">
              ShopPilot helps e-commerce brands automate support, answer customers instantly, and take action across your tools — all in one system.
            </p>

            {/* CTAs */}
            <div className="mb-12 flex flex-wrap items-center gap-4">
              <button className="inline-flex h-14 items-center justify-center gap-2 rounded-xl bg-[#151515] px-7 text-[15px] font-semibold text-white shadow-xl shadow-black/10 transition-all hover:bg-black hover:scale-[1.02] active:scale-[0.98]">
                Get started
                <ArrowRight size={18} />
              </button>

              <button className="inline-flex h-14 items-center justify-center gap-2 rounded-xl border-2 border-black/10 bg-white/50 px-7 text-[15px] font-semibold text-black backdrop-blur-sm transition-all hover:bg-white hover:border-black/20 active:scale-[0.98]">
                See how it works
              </button>
            </div>

            {}
            {/* Feature list */}
            <div className="space-y-4">
              {features.map((feature) => (
                <div key={feature} className="flex items-center gap-3 text-[15px] font-medium text-gray-700">
                  <CheckCircle2 size={20} className="text-[#8C7A6B] shrink-0" strokeWidth={2.5} />
                  <span>{feature}</span>
                </div>
              ))}
            </div>
          </div>

          {/* =========================================================
              RIGHT COLUMN: DASHBOARD 3D TILT
             ========================================================= */}
          <div className="w-full lg:w-[55%] relative mt-10 lg:mt-0 lg:absolute lg:right-[-5%] xl:right-[-2%] lg:top-1/2 lg:-translate-y-1/2 pointer-events-none">
            
            {/* 3D Perspective Wrapper */}
            <div 
              className="relative mx-auto lg:ml-auto w-full max-w-190"
              style={{ perspective: "2000px" }}
            >
              {/* Tilted Product Dashboard */}
              <div 
               className="relative origin-center lg:origin-left transition-transform duration-1000 ease-out scale-[0.75] sm:scale-[0.82] lg:scale-[0.88] xl:scale-[0.94]"
                style={{
                  transform: "rotateY(-12deg) rotateX(4deg) rotateZ(-2deg)",
                  transformStyle: "preserve-3d",
                }}
              >
                {/* Glow/Shadow behind the dashboard */}
                <div className="absolute inset-0 bg-black/15 blur-[50px] rounded-[30px] transform translate-y-8" />
                
                {/* The Dashboard Mockup */}
                <div className="relative z-10 pointer-events-auto">
                  <DashboardMockup />
                </div>
                
                {/* Subtle glare effect */}
                <div className="absolute inset-0 z-20 bg-gradient-to-br from-white/20 via-transparent to-black/5 pointer-events-none rounded-xl" />
              </div>

              {}
              {/* =====================================================
                  HANDWRITTEN NOTE
                 ===================================================== */}
              <div className="absolute -bottom-16 sm:-bottom-24 left-[5%] sm:left-[25%] lg:left-[15%] z-30 transform -rotate-6 hidden sm:block">
                <div className="relative">
                  {/* Hand-drawn SVG Arrow */}
                  <svg
                    viewBox="0 0 120 75"
                    className="absolute -right-12 -top-14 h-[60px] w-[90px] rotate-[15deg] text-gray-800 drop-shadow-sm"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d="M103 4C91 27 73 45 34 62"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                    />
                    <path
                      d="M34 62L41 51"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                    />
                    <path
                      d="M34 62L47 61"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                    />
                  </svg>

                  {/* Handwritten Text */}
                  <p
                    className="text-gray-900 text-lg md:text-xl font-medium"
                    style={{
                      fontFamily: '"Caveat", "Comic Sans MS", "Bradley Hand", cursive',
                      lineHeight: "1.2",
                      textShadow: "1px 1px 0px rgba(255,255,255,0.7)"
                    }}
                  >
                    Real answers.<br />
                    From your data.
                  </p>
                </div>
              </div>
              
            </div>
          </div>



        </div>

      </div>

    </section>

  );

}