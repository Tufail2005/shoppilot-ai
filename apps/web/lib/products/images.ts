/**
 * Seed SKU literals — canonical list from `supabase/seed.sql`
 * (products INSERT block). Keep in sync with the seed data.
 */
export const SEED_SKUS = [
  "TSHIRT-001",
  "TSHIRT-002",
  "JEANS-001",
  "HOODIE-001",
  "SHOE-001",
  "SHOE-002",
  "BAG-001",
  "CAP-001",
  "BELT-001",
  "WALLET-001",
  "JACKET-001",
  "SHORTS-001",
  "SOCKS-001",
  "WATCH-001",
  "SUN-001",
  "PERF-001",
  "CARE-001",
  "CARE-002",
  "HOME-001",
  "HOME-002",
] as const;

export type SkuLiteral = (typeof SEED_SKUS)[number];

/**
 * Partial SKU → local product photo mapping.
 *
 * Only SKUs whose photos were downloaded AND visually verified against the
 * product (name/description/category) get an entry here. Unmapped SKUs stay
 * unmapped on purpose (deleted key = placeholder, never a guessed path).
 *
 * Per-photo source, creator and license verification:
 * apps/web/public/products/IMAGE_SOURCES.md
 */
const PRODUCT_IMAGES: Partial<Record<SkuLiteral, string>> = {
  "TSHIRT-001": "/products/tshirt-001.webp",
  "TSHIRT-002": "/products/tshirt-002.webp",
  "JEANS-001": "/products/jeans-001.webp",
  "HOODIE-001": "/products/grey-hoo-001.webp",
  "SHOE-001": "/products/shoe-001.webp",
  "SHOE-002": "/products/shoe-002.webp",
  "BAG-001": "/products/bag-001.webp",
  "CAP-001": "/products/cap-001.webp",
  "BELT-001": "/products/belt-001.webp",
  "WALLET-001": "/products/wallet-001.webp",
  "JACKET-001": "/products/jacket-001.png",
  "SHORTS-001": "/products/shorts-001.webp",
  "SOCKS-001": "/products/socks-001.webp",
  "WATCH-001": "/products/watch-001.webp",
  "SUN-001": "/products/sun-001.webp",
  "PERF-001": "/products/perf-001.webp",
  "CARE-001": "/products/care-001.webp",
  "CARE-002": "/products/care-002.webp",
  "HOME-001": "/products/home-001.webp",
  "HOME-002": "/products/home-002.webp",
};

/**
 * Resolve the local image for a SKU that is expected to be one of the seeded
 * SKUs. Returns null if the SKU has no verified photo (placeholder is
 * rendered by the caller) or if the SKU is unknown to the seed set at all.
 */
export function getProductImage(sku: string): string | null {
  if (!Object.hasOwn(PRODUCT_IMAGES, sku)) return null;
  return PRODUCT_IMAGES[sku as SkuLiteral] ?? null;
}
