"use client";

import Image from "next/image";
import { useState } from "react";

/**
 * Hand-coded neutral placeholder (inline SVG, no image file) for SKUs
 * without a verified photo, and for <img> load failures at runtime.
 * Deliberately NOT an SVG file via next/image — the optimizer blocks SVGs.
 */
function ProductPlaceholder() {
  return (
    <div
      aria-hidden="true"
      className="absolute inset-0 flex items-center justify-center bg-[#ece8e0]"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="#b7afa2"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="size-12"
      >
        {/* Hand-drawn product box: lid, body, and strap */}
        <path d="M21 8l-9-5-9 5 9 5 9-5z" />
        <path d="M3 8v8l9 5 9-5V8" />
        <path d="M7.5 5.5l9 5M12 13v7" />
      </svg>
    </div>
  );
}

/**
 * Product photo with runtime fallback:
 * - verified photo (src) rendered through next/image
 * - broken src or unmapped SKU → hand-coded placeholder
 */
export function ProductImage({ src, alt }: { src: string | null; alt: string }) {
  const [failed, setFailed] = useState(false);

  if (!src || failed) {
    return <ProductPlaceholder />;
  }

  return (
    <Image
      src={src}
      alt={alt}
      fill
      sizes="(max-width: 640px) 100vw, (max-width: 1024px) 45vw, 30vw"
      onError={() => setFailed(true)}
      className="object-cover"
    />
  );
}
