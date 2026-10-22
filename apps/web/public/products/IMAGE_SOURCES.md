# Product Image Sources & License Ledger

Real product photography for the `/products` listing (Phase 4 demo images).

## Policy

- Photographs only from Pexels (`pexels.com`). No AI-generated or synthetic
  images are used anywhere in this project.
- All photos are licensed under the Pexels License, verified firsthand at
  **https://www.pexels.com/license/** (fetched 2026-10-22): Pexels photos are
  free to use, modification is allowed, and attribution is not required (but
  creators are credited below anyway).
- Individual photos were selected preferring logo-free / product-only shots.
  Minor compromises are flagged per row.
- Files are square 512×512 center-crop variants delivered as WebP
  (`?auto=compress&cs=tinysrgb&w=512&h=512&fit=crop`, requested with
  `Accept: image/webp`). They serve from `apps/web/public/products/`.
  Visual framing (square container + `object-cover`) is handled in
  `apps/web/app/components/products/product-image.tsx`.
- Pexels photo page URLs resolve by numeric photo ID; the `/photo/<slug>-<id>/`
  form stays valid even if the slug redirects to the canonical title.
- Coverage rule enforced by this ledger: 20 seeded SKUs, 19 mapped to files,
  1 documented fallback (placeholder). No orphan files.

## Image verification matrix

Verified: all rows below. License page verified 2026-10-22 for all photos.
"Page-verified" = individual photo page fetched firsthand this session
(creator display name + per-photo license status read from the page).

| SKU | Local file | Source page | Creator | License | Verified | Notes |
|---|---|---|---|---|---|---|
| TSHIRT-001 | `tshirt-001.webp` | https://www.pexels.com/photo/34156906/ | dayong tien | https://www.pexels.com/license/ | 2026-10-22 (search-page metadata) | Flat lay of cotton tees, black + mint. Logo-free. |
| TSHIRT-002 | `tshirt-002.webp` | https://www.pexels.com/photo/white-t-shirt-hanging-on-a-hanger-18257675/ | dayong tien | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | White crew-neck tee on hanger, Shanghai boutique. Logo-free (tiny neck label text). |
| JEANS-001 | `jeans-001.webp` | https://www.pexels.com/photo/24513278/ | Badis Benkhelil | https://www.pexels.com/license/ | 2026-10-22 (search-page metadata) | Folded blue denim. ⚠ Minor: small "GeMi MILANO" patch label visible. |
| HOODIE-001 | — (placeholder) | — | — | — | 2026-10-22 | **Fallback.** No logo-free product-only photo of a gray hoodie found on Pexels; all search results were either branded (e.g. 6186448 "Supreme"), abandoned (11059826 brand patch), a garment mismatch (9594147 "CLOTH & Co" tag; 9594089 crew-neck sweatshirt, white) or lifestyle shots of people. Falls back to the neutral hand-coded placeholder by design. |
| SHOE-001 | `shoe-001.webp` | https://www.pexels.com/photo/white-sneakers-on-the-floor-11513443/ | Clarisse Vidal | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Pair of white sneakers against olive wall. ⚠ Subject small in frame; slight wear visible. |
| SHOE-002 | `shoe-002.webp` | https://www.pexels.com/photo/new-white-running-shoes-19537610/ | Ahimsa - OM | https://www.pexels.com/license/ | 2026-10-22 (page content via search result render) | White/gray knit running trainers, logo-free. |
| BAG-001 | `bag-001.webp` | https://www.pexels.com/photo/3731256/ | Luis Quintero | https://www.pexels.com/license/ | 2026-10-22 (page content via search result render) | Black backpack against white background, logo-free. ⚠ A hand holds it. |
| CAP-001 | `cap-001.webp` | https://www.pexels.com/photo/26956138/ | mohamed abdelghaffar | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Black/white trucker cap, studio shot. ⚠ Decorative patch graphic (not a brand mark). |
| BELT-001 | `belt-001.webp` | https://www.pexels.com/photo/89783/ | Pixabay | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Black leather belt with metal buckle on denim. Legacy CDN filename (`belts-belt-buckle-leather-metal-89783.jpeg`). |
| WALLET-001 | `wallet-001.webp` | https://www.pexels.com/photo/27467366/ | Gül Işık | https://www.pexels.com/license/ | 2026-10-22 (page content via search result render) | Tan slim leather wallet on white. ⚠ Tiny embossed monogram. |
| JACKET-001 | `jacket-001.webp` | https://www.pexels.com/photo/close-up-of-jeans-jacket-6843231/ | Antoni Shkraba | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Blue denim jacket collar on white background, collar patch is blank leather (unbranded). |
| SHORTS-001 | `shorts-001.webp` | https://www.pexels.com/photo/30710089/ | Luis Quintero (@jibarofoto) | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Flat lay of men's chino shorts. ⚠ Multi-colored, not a single khaki pair. |
| SOCKS-001 | `socks-001.webp` | https://www.pexels.com/photo/5746091/ | Ryutaro Tsukata | https://www.pexels.com/license/ | 2026-10-22 (page content via search result render) | Multiple ribbed sock pairs, logo-free. |
| WATCH-001 | `watch-001.webp` | https://www.pexels.com/photo/silver-wristwatch-on-white-background-20022436/ | Olga Kovalski | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Minimalist watch, mesh strap, white surface. Dial unbranded. |
| SUN-001 | `sun-001.webp` | https://www.pexels.com/photo/sunglasses-on-a-marble-surface-14090246/ | Nurgül Kelebek | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Sleek black sunglasses on marble, logo-free. |
| PERF-001 | `perf-001.webp` | https://www.pexels.com/photo/white-perfume-bottle-3785784/ | Castorly Stock | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Minimalist white spray bottle on gray. Unbranded. |
| CARE-001 | `care-001.webp` | https://www.pexels.com/photo/white-cosmetic-plastic-pump-bottle-with-beige-background-7814990/ | Mikhail Nilov | https://www.pexels.com/license/ | 2026-10-22 (page content via search result render) | White pump bottle, blank label, warm background. Face/cleanser suitable. |
| CARE-002 | `care-002.webp` | https://www.pexels.com/photo/6690236/ | Tara Winstead | https://www.pexels.com/license/ | 2026-10-22 (page content via search result render) | White cream jar, top-down, logo-free. |
| HOME-001 | `home-001.webp` | https://www.pexels.com/photo/19155039/ | Osman Karagöz | https://www.pexels.com/license/ | 2026-10-22 (page-verified) | Stainless steel travel mug on wooden table. ⚠ Tiny side text on mug. |
| HOME-002 | `home-002.webp` | https://www.pexels.com/photo/1112598/ | Eneida Nieves | https://www.pexels.com/license/ | 2026-10-22 (page content via search result render) | Black desk lamp with succulent, logo-free. |

Legend: "page-verified" = photo page fetched firsthand and creator/license read
from it; "page content via search result render" = page content (creator,
license status, dimensions, description) surfaced through search-engine render
of the same photo page; "search-page metadata" = photo ID and creator slug read
from Pexels' own search page (download link `dl=pexels-<creator>-<id>.jpg`).

## Rejected candidates (used for wasted-look reasons only)

Every rejection below was downloaded at square-crop size and visually
inspect-checked before rejection:

- 37704839 (Daniel & Hannah Snipes) — center crop reads as crumpled fabric, not a tee → replaced by 18257675
- 11059826 (30Nudos Adicora) — folded garment with prominent brand patch
- 18404159 (creator slug `marsianin`) — pile of colorful hoodies outdoors, no gray product shot
- 6186448 (introspectivedsgn) — gray hoodie but large Supreme chest logo
- 9594147 / 9594089 (Ron Lach) — "CLOTH & Co" hang tag / crew-neck sweatshirt (no hood, white)
- 2529148 (Melvin Buezo) — Nike swoosh clearly visible
- 1456733 (Ray Piedra) — Adidas three-stripes tongue logo
- 11946030 — "NIKE AIR" tongue print
- 11096066 (Mikael Monjour) — Levi's Strauss & Co red tab + brand patch
- 38561616 (_ Whittington) — ambiguous blurry denim close-up, not recognizable as a jacket
- 22032436 (Laura Chouette) — Daniel Wellington branding on dial
- 23338519 (Garfield Besa) — small brand text on dial
- 5465834 — tiny lens text, second-day look → replaced by 14090246
- 13430498 (Saliem Moeslan) — Tom Ford logos on temple
- 12053222 (Albina White) — "Zielinski & Ro…" branded label
- 6689393 (Jana Kukebal) — MONAT branding on tube → replaced by 7814990

## Mapping ↔ file bijection check

Files in this directory (19 `.webp`) map 1:1 into the 19 mapped SKUs below;
`HOODIE-001` renders the hand-coded placeholder from
`apps/web/lib/products/images.ts` + `apps/web/app/components/products/product-image.tsx`.

`tshirt-001 tshirt-002 jeans-001 shoe-001 shoe-002 bag-001 cap-001 belt-001
wallet-001 jacket-001 shorts-001 socks-001 watch-001 sun-001 perf-001
care-001 care-002 home-001 home-002`
