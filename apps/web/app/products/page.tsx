import Link from "next/link";
import { redirect } from "next/navigation";

import { Navbar } from "@/app/components/home/navbar";
import { ProductImage } from "@/app/components/products/product-image";
import { ApiError, getProducts, type ProductList } from "@/lib/api";
import { getProductImage } from "@/lib/products/images";

const PAGE_SIZE = 5;

// Category names match the seeded data (supabase/seed.sql).
const CATEGORIES = [
  "Clothing",
  "Footwear",
  "Accessories",
  "Beauty",
  "Home",
] as const;

type QueryParamValue = string | string[] | undefined;

/**
 * Strict offset validation: the complete string must match a canonical
 * non-negative integer, be a safe number, and be page-size aligned.
 * Anything else (e.g. "1.5", "3abc", negative, empty, repeated param)
 * falls back to page 1.
 */
function parseOffset(raw: QueryParamValue, pageSize: number): number {
  if (typeof raw !== "string") return 0;
  if (!/^(0|[1-9]\d*)$/.test(raw)) return 0;

  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 0) return 0;

  return value % pageSize === 0 ? value : 0;
}

/**
 * Next.js delivers repeated ?category=a&category=b as an array;
 * only a single non-empty string is accepted.
 */
function normalizeCategory(raw: QueryParamValue): string | undefined {
  return typeof raw === "string" && raw !== "" ? raw : undefined;
}

/**
 * Build a /products URL from the normalized values.
 * URLSearchParams guarantees query encoding for both API calls and links.
 */
function buildProductsUrl(category: string | undefined, offset: number): string {
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  if (offset > 0) params.set("offset", String(offset));

  const query = params.toString();
  return query ? `/products?${query}` : "/products";
}

export default async function ProductsPage({
  searchParams,
}: PageProps<"/products">) {
  const { category, offset } = await searchParams;

  const currentOffset = parseOffset(offset, PAGE_SIZE);
  const currentCategory = normalizeCategory(category);

  let data: ProductList;
  try {
    data = await getProducts({
      limit: PAGE_SIZE,
      ...(currentOffset > 0 && { offset: currentOffset }),
      ...(currentCategory && { category: currentCategory }),
    });
  } catch (error) {
    // Non-2xx responses keep their HTTP status; everything else
    // (fetch rejection) is a network-level failure.
    const detail =
      error instanceof ApiError
        ? `HTTP ${error.status}. The API returned an error.`
        : "The API is unreachable.";

    return (
      <main className="min-h-screen bg-[#f7f5f0] text-[#171717]">
        <Navbar />
        <div className="mx-auto max-w-[1440px] px-5 py-16 sm:px-8 lg:px-12">
          <h1 className="text-2xl font-semibold tracking-tight">Products</h1>
          <div className="mt-6 rounded-xl border border-black/10 bg-white p-8">
            <p className="font-medium">Failed to load products.</p>
            <p className="mt-1 text-sm text-black/60">{detail}</p>
            <Link
              href="/products"
              className="mt-4 inline-block text-sm font-medium text-black/70 underline underline-offset-4 transition hover:text-black"
            >
              Try again
            </Link>
          </div>
        </div>
      </main>
    );
  }

  // redirect() signals by throwing internally, so it must stay outside
  // the try/catch above; the API-error catch block no longer runs here.

  // Genuinely empty dataset: render the empty state (no redirect — a
  // redirect here would hide the meaningful empty result or loop).
  if (data.total === 0) {
    return (
      <main className="min-h-screen bg-[#f7f5f0] text-[#171717]">
        <Navbar />
        <div className="mx-auto max-w-[1440px] px-5 py-16 sm:px-8 lg:px-12">
          <h1 className="text-2xl font-semibold tracking-tight">Products</h1>
          <div className="mt-6 rounded-xl border border-black/10 bg-white p-8 text-center">
            <p className="font-medium">No products found</p>
            <p className="mt-1 text-sm text-black/60">
              There are no products in the catalog for this filter.
            </p>
            <Link
              href="/products"
              className="mt-4 inline-block text-sm font-medium text-black/70 underline underline-offset-4 transition hover:text-black"
            >
              View all products
            </Link>
          </div>
        </div>
      </main>
    );
  }

  // Positive offset beyond the last valid page: redirect to the last
  // valid page, preserving the normalized category. No clamping.
  if (currentOffset >= data.total) {
    const lastOffset = Math.floor((data.total - 1) / PAGE_SIZE) * PAGE_SIZE;
    redirect(buildProductsUrl(currentCategory, lastOffset));
  }

  const from = data.offset + 1;
  const to = data.offset + data.items.length;
  const hasNext = data.offset + PAGE_SIZE < data.total;
  const hasPrev = data.offset > 0;

  return (
    <main className="min-h-screen bg-[#f7f5f0] text-[#171717]">
      <Navbar />

      <div className="mx-auto max-w-[1440px] px-5 py-10 sm:px-8 lg:px-12">
        <h1 className="text-2xl font-semibold tracking-tight">Products</h1>
        <p className="mt-1 text-sm text-black/60">
          Showing {from}&ndash;{to} of {data.total}
        </p>

        {/* Category filter — selecting a category resets the offset to page 1 */}
        <div className="mt-6 flex flex-wrap items-center gap-2">
          <Link
            href="/products"
            className={`rounded-full px-3.5 py-1.5 text-sm font-medium transition ${
              currentCategory
                ? "text-black/60 hover:bg-black/5 hover:text-black"
                : "bg-[#171717] text-white"
            }`}
          >
            All
          </Link>
          {CATEGORIES.map((name) => (
            <Link
              key={name}
              href={name === currentCategory ? "/products" : buildProductsUrl(name, 0)}
              className={`rounded-full px-3.5 py-1.5 text-sm font-medium transition ${
                currentCategory === name
                  ? "bg-[#171717] text-white"
                  : "text-black/60 hover:bg-black/5 hover:text-black"
              }`}
            >
              {name}
            </Link>
          ))}
        </div>

        <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.items.map((product) => (
            <div
              key={product.id}
              className="flex flex-col rounded-xl border border-black/10 bg-white p-5"
            >
              <div className="relative mb-4 aspect-square overflow-hidden rounded-lg bg-[#f7f5f0]">
                <ProductImage
                  src={getProductImage(product.sku)}
                  alt={product.name}
                />
              </div>

              <div className="flex items-start justify-between gap-3">
                <h2 className="font-semibold tracking-tight">{product.name}</h2>
                <span className="rounded-full bg-black/5 px-2.5 py-0.5 text-xs font-medium text-black/70">
                  {product.sku}
                </span>
              </div>

              {product.description && (
                <p className="mt-1 text-sm text-black/60">{product.description}</p>
              )}

              <div className="mt-auto flex items-center justify-between pt-4">
                <span className="font-semibold">{product.price}</span>
                <span className="text-xs text-black/60">
                  {product.stock} in stock
                </span>
              </div>

              {product.category && (
                <p className="mt-1 text-xs text-black/50">{product.category}</p>
              )}
            </div>
          ))}
        </div>

        {/* Pagination — category is preserved in every link */}
        <div className="mt-8 flex items-center justify-between border-t border-black/10 pt-4">
          {hasPrev ? (
            <Link
              href={buildProductsUrl(currentCategory, data.offset - PAGE_SIZE)}
              className="text-sm font-medium text-black/70 transition hover:text-black"
            >
              &larr; Previous
            </Link>
          ) : (
            <span />
          )}

          {hasNext ? (
            <Link
              href={buildProductsUrl(currentCategory, data.offset + PAGE_SIZE)}
              className="text-sm font-medium text-black/70 transition hover:text-black"
            >
              Next &rarr;
            </Link>
          ) : (
            <span />
          )}
        </div>
      </div>
    </main>
  );
}
