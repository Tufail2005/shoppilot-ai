import type { Metadata } from "next";

import {SignupForm} from "./signup-form";

export default function SignupPage() {
  return (
    <main className="min-h-screen bg-slate-950">
      <div className="grid min-h-screen lg:grid-cols-2">
        {/* Left — Branding */}
        <div className="relative hidden overflow-hidden lg:flex">
          <div className="absolute inset-0 bg-gradient-to-br from-indigo-950 via-slate-900 to-slate-950" />

          <div className="relative z-10 flex w-full flex-col justify-between p-10">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-white text-sm font-bold text-slate-950">
                SP
              </div>

              <span className="text-lg font-semibold text-white">
                ShopPilot AI
              </span>
            </div>

            <div className="max-w-lg">
              <div className="mb-6 inline-flex items-center rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-slate-300">
                <span className="mr-2 h-2 w-2 rounded-full bg-indigo-400" />
                Built for modern support teams
              </div>

              <h1 className="text-5xl font-semibold leading-tight tracking-tight text-white">
                Your support team,{" "}
                <span className="text-indigo-400">supercharged by AI.</span>
              </h1>

              <p className="mt-6 max-w-md text-base leading-7 text-slate-400">
                Connect your store, give your AI access to your knowledge, and
                build a better customer support experience.
              </p>

              <div className="mt-10 grid grid-cols-2 gap-3">
                {[
                  "AI customer support",
                  "Product knowledge",
                  "Order assistance",
                  "Human escalation",
                ].map((feature) => (
                  <div
                    key={feature}
                    className="rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-slate-300"
                  >
                    <span className="mr-2 text-indigo-400">✓</span>
                    {feature}
                  </div>
                ))}
              </div>
            </div>

            <p className="text-xs text-slate-600">
              © {new Date().getFullYear()} ShopPilot AI
            </p>
          </div>
        </div>

        {/* Right — Signup */}
        <div className="flex min-h-screen items-center justify-center bg-slate-50 px-5 py-10">
          <div className="w-full max-w-md">
            <div className="mb-10 flex items-center justify-center gap-3 lg:hidden">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-950 text-sm font-bold text-white">
                SP
              </div>

              <span className="text-lg font-semibold text-slate-950">
                ShopPilot AI
              </span>
            </div>

            <div className="rounded-2xl border border-slate-200 bg-white p-7 shadow-sm sm:p-9">
              <div className="mb-8">
                <h2 className="text-2xl font-semibold tracking-tight text-slate-950">
                  Create your account
                </h2>

                <p className="mt-2 text-sm leading-6 text-slate-500">
                  Get started with ShopPilot AI.
                </p>
              </div>

              <SignupForm />

              <div className="mt-6 text-center">
                <p className="text-sm text-slate-500">
                  Already have an account?{" "}
                  <a
                    href="/login"
                    className="font-medium text-slate-950 underline-offset-4 hover:underline"
                  >
                    Sign in
                  </a>
                </p>
              </div>
            </div>

            <div className="mt-6 text-center">
              <a
                href="/"
                className="text-sm text-slate-500 hover:text-slate-950"
              >
                ← Back to home
              </a>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}