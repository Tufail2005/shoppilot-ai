const API_URL =
  process.env.API_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000";


export async function getApiHealth() {
  const response = await fetch(`${API_URL}/health`, {
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error("API health check failed");
  }

  return response.json() as Promise<{
    status: string;
  }>;
}

export type Product = {
  id: string; // uuid
  sku: string;
  name: string;
  description: string | null;
  // The backend serializes Decimal as a 2-decimal string (e.g. "19.99");
  // no currency convention is documented, so this value is not formatted further.
  price: string;
  stock: number;
  category: string | null;
  created_at: string; // ISO datetime
  updated_at: string; // ISO datetime
};

export type ProductList = {
  items: Product[];
  total: number;
  limit: number;
  offset: number;
};

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`API error ${status}`);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function getProducts(params: {
  limit?: number;
  offset?: number;
  category?: string;
}): Promise<ProductList> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.offset !== undefined && params.offset > 0) {
    search.set("offset", String(params.offset));
  }
  if (params.category) {
    search.set("category", params.category);
  }

  const query = search.toString();
  const response = await fetch(`${API_URL}/products${query ? `?${query}` : ""}`, {
    cache: "no-store",
  });

  if (!response.ok) {
    throw new ApiError(response.status);
  }

  return response.json() as Promise<ProductList>;
}