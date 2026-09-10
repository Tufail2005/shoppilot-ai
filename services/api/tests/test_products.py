CLOTHING_PRODUCT_ID = "10000000-0000-0000-0000-000000000001"
CLOTHING_SKU = "TSHIRT-001"
UNKNOWN_PRODUCT_ID = "99999999-9999-9999-9999-999999999999"


class TestProductList:
    def test_list_returns_all_seeded_products(self, client):
        response = client.get("/products")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 20
        assert len(body["items"]) == 20
        assert body["limit"] == 20
        assert body["offset"] == 0

    def test_limit_5_returns_5_items_and_total_20(self, client):
        response = client.get("/products", params={"limit": 5})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 20
        assert len(body["items"]) == 5
        assert body["limit"] == 5

    def test_offset_paging_disjoint_pages(self, client):
        page_one = client.get("/products", params={"limit": 10, "offset": 0})
        page_two = client.get("/products", params={"limit": 10, "offset": 10})

        assert page_one.status_code == 200
        assert page_two.status_code == 200

        ids_page_one = {item["id"] for item in page_one.json()["items"]}
        ids_page_two = {item["id"] for item in page_two.json()["items"]}

        assert len(ids_page_one) == 10
        assert len(ids_page_two) == 10
        assert ids_page_one.isdisjoint(ids_page_two)

    def test_offset_paging_last_page_exact_rows(self, client):
        full = client.get("/products", params={"limit": 100})
        page = client.get("/products", params={"limit": 100, "offset": 15})

        assert full.status_code == 200
        assert page.status_code == 200

        full_ids = [item["id"] for item in full.json()["items"]]
        page_ids = [item["id"] for item in page.json()["items"]]

        assert page_ids == full_ids[15:]

    def test_category_filter_clothing_only(self, client):
        response = client.get("/products", params={"category": "Clothing"})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 7
        assert len(body["items"]) == 7
        assert all(item["category"] == "Clothing" for item in body["items"])

    def test_unknown_category_returns_200_empty_items(self, client):
        response = client.get("/products", params={"category": "Nonexistent"})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["items"] == []

    def test_limit_0_returns_422(self, client):
        response = client.get("/products", params={"limit": 0})

        assert response.status_code == 422

    def test_limit_1000_returns_422(self, client):
        response = client.get("/products", params={"limit": 1000})

        assert response.status_code == 422

    def test_offset_negative_returns_422(self, client):
        response = client.get("/products", params={"offset": -1})

        assert response.status_code == 422

    def test_price_is_decimal_string(self, client):
        response = client.get("/products", params={"limit": 5})

        assert response.status_code == 200
        for item in response.json()["items"]:
            assert isinstance(item["price"], str)
            assert len(item["price"].split(".")[-1]) == 2


class TestProductDetail:
    def test_known_seeded_product_returns_exact_row(self, client):
        response = client.get(f"/products/{CLOTHING_PRODUCT_ID}")

        assert response.status_code == 200
        product = response.json()
        assert product["id"] == CLOTHING_PRODUCT_ID
        assert product["sku"] == CLOTHING_SKU
        assert product["name"] == "Classic Black T-Shirt"
        assert product["description"] == "Cotton everyday t-shirt"
        assert product["price"] == "19.99"
        assert product["stock"] == 100
        assert product["category"] == "Clothing"

    def test_unknown_uuid_returns_404(self, client):
        response = client.get(f"/products/{UNKNOWN_PRODUCT_ID}")

        assert response.status_code == 404
        assert response.json() == {"detail": "Product not found"}

    def test_malformed_uuid_returns_422(self, client):
        response = client.get("/products/not-a-uuid")

        assert response.status_code == 422


class TestHealthRegression:
    def test_health_still_works(self, client):
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_db_still_works(self, client):
        response = client.get("/health/db")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}
