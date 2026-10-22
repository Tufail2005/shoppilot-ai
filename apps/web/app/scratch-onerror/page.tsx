// One-shot scratch test ONLY — deleted after verification. Not part of the repo.
import { ProductImage } from "@/app/components/products/product-image";

export default function ScratchOnErrorPage() {
  return (
    <main>
      <div className="relative aspect-square">
        <ProductImage src="/products/does-not-exist.webp" alt="broken test" />
      </div>
    </main>
  );
}
