import type { Metadata } from "next";

import { LoginForm } from "./login-form";

export const metadata: Metadata = {
  title: "Log in",
  description: "Log in to your ShopPilot AI account.",
};

export default function LoginPage() {
  return (
    <main className="min-h-screen bg-slate-950">
      <div className="grid min-h-screen lg:grid-cols-2">
        {/* Left — Branding */}
        <div className="relative hidden overflow-hidden lg:flex">
          <div className="absolute inset-0 bg-gradient-to-br from-slate-950 via-slate-900 to-indigo-950" />

          <div className="relative z-10 flex w-full flex-col justify-between p-10">
            {/* Logo */}
            <div>
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-white text-sm font-bold text-slate-950">
                  SP
                </div>

                <span className="text-lg font-semibold text-white">
                  ShopPilot AI
                </span>
              </div>
            </div>

            {/* Main message */}
            <div className="max-w-lg">
              <div className="mb-6 inline-flex items-center rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-slate-300 backdrop-blur">
                <span className="mr-2 h-2 w-2 rounded-full bg-emerald-400" />
                AI-powered customer support
              </div>

              <h1 className="text-5xl font-semibold leading-tight tracking-tight text-white">
                Turn customer conversations into{" "}
                <span className="text-indigo-400">better experiences.</span>
              </h1>

              <p className="mt-6 max-w-md text-base leading-7 text-slate-400">
                Let AI handle customer questions, understand your products,
                and help your support team resolve issues faster.
              </p>

              {/* Small dashboard preview */}
              <div className="mt-10 rounded-2xl border border-white/10 bg-white/5 p-4 shadow-2xl backdrop-blur">
                <div className="mb-4 flex items-center justify-between">
                  <div>
                    <p className="text-xs text-slate-500">AI Assistant</p>
                    <p className="mt-1 text-sm font-medium text-white">
                      Customer conversation
                    </p>
                  </div>

                  <span className="rounded-full bg-emerald-400/10 px-2.5 py-1 text-xs text-emerald-400">
                    Active
                  </span>
                </div>

                <div className="space-y-3">
                  <div className="ml-auto max-w-[75%] rounded-xl rounded-br-sm bg-indigo-500 px-4 py-3 text-sm text-white">
                    Where is my order?
                  </div>

                  <div className="max-w-[80%] rounded-xl rounded-bl-sm bg-white/10 px-4 py-3 text-sm leading-6 text-slate-300">
                    I found your order. It is currently in transit and should
                    arrive tomorrow.
                  </div>
                </div>
              </div>
            </div>

            <p className="text-xs text-slate-600">
              © {new Date().getFullYear()} ShopPilot AI
            </p>
          </div>
        </div>

        {/* Right — Login */}
        <div className="flex min-h-screen items-center justify-center bg-slate-50 px-5 py-10">
          <div className="w-full max-w-md">
            {/* Mobile logo */}
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
                  Welcome back
                </h2>

                <p className="mt-2 text-sm leading-6 text-slate-500">
                  Sign in to your ShopPilot account.
                </p>
              </div>

              <LoginForm />

              <div className="mt-6 text-center">
                <p className="text-sm text-slate-500">
                  Don&apos;t have an account?{" "}
                  <a
                    href="/signup"
                    className="font-medium text-slate-950 underline-offset-4 hover:underline"
                  >
                    Create one
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
