"""
Tests for restocking API endpoints.
"""
from datetime import datetime, timedelta


class TestRestockingEndpoints:
    """Test suite for restock recommendations and restock order submission."""

    # Recommendation endpoint

    def test_recommendations_stay_within_budget(self, client):
        """Test that recommended items never cost more than the budget."""
        response = client.get("/api/restocking/recommendations", params={"budget": 10000})
        assert response.status_code == 200

        data = response.json()
        assert data["spent"] <= data["budget"]
        assert data["remaining"] == round(data["budget"] - data["spent"], 2)

        for item in data["items"]:
            assert item["recommended_quantity"] > 0
            assert abs(item["line_total"] - item["recommended_quantity"] * item["unit_cost"]) < 0.01

    def test_recommendations_exclude_falling_demand(self, client):
        """Test that items with falling forecast demand are not restocked (MTR-304 falls from 50 to 35)."""
        response = client.get("/api/restocking/recommendations", params={"budget": 100000})
        skus = [item["sku"] for item in response.json()["items"]]
        assert "MTR-304" not in skus

    def test_recommendations_exclude_fully_stocked_items(self, client):
        """Test that items with enough stock to cover the forecast are not restocked (PSU-501 has 420 on hand vs 252 forecast)."""
        response = client.get("/api/restocking/recommendations", params={"budget": 100000})
        skus = [item["sku"] for item in response.json()["items"]]
        assert "PSU-501" not in skus

    def test_recommendations_priority_order(self, client):
        """Test that items with faster demand growth come first, using the demand forecast as the source."""
        demand = client.get("/api/demand").json()
        growth = {d["item_sku"]: d["forecasted_demand"] / d["current_demand"] for d in demand}

        response = client.get("/api/restocking/recommendations", params={"budget": 100000})
        item_growth = [growth[item["sku"]] for item in response.json()["items"]]

        assert item_growth == sorted(item_growth, reverse=True)

    def test_recommendations_partial_fill(self, client):
        """Test that a small budget buys as many units of the top item as it covers (WDG-001 at $12.50, budget $100 gives 8 units)."""
        response = client.get("/api/restocking/recommendations", params={"budget": 100})
        data = response.json()

        assert len(data["items"]) == 1
        assert data["items"][0]["sku"] == "WDG-001"
        assert data["items"][0]["recommended_quantity"] == 8
        assert data["spent"] == 100.0
        assert data["remaining"] == 0.0

    def test_recommendations_zero_budget(self, client):
        """Test that a zero budget recommends nothing."""
        response = client.get("/api/restocking/recommendations", params={"budget": 0})
        data = response.json()

        assert data["items"] == []
        assert data["spent"] == 0.0

    def test_recommendations_max_budget_independent_of_budget(self, client):
        """Test that max_budget is the same for any requested budget, so the slider maximum is stable."""
        low = client.get("/api/restocking/recommendations", params={"budget": 0}).json()
        high = client.get("/api/restocking/recommendations", params={"budget": 10000}).json()

        assert low["max_budget"] == high["max_budget"]
        assert high["max_budget"] > 0

    def test_recommendations_flag_items_without_stock_record(self, client):
        """Test that forecast SKUs with no inventory row are treated as zero stock and flagged."""
        response = client.get("/api/restocking/recommendations", params={"budget": 100000})
        wdg = next(item for item in response.json()["items"] if item["sku"] == "WDG-001")

        assert wdg["has_stock_record"] is False
        assert wdg["on_hand"] == 0

    def test_recommendations_require_budget(self, client):
        """Test that the budget query parameter is required."""
        response = client.get("/api/restocking/recommendations")
        assert response.status_code == 422

    def test_recommendations_reject_negative_budget(self, client):
        """Test that a negative budget is rejected."""
        response = client.get("/api/restocking/recommendations", params={"budget": -1})
        assert response.status_code == 422

    def test_recommendations_reject_infinite_budget(self, client):
        """Test that an infinite budget is rejected, since it cannot be serialized in the response."""
        response = client.get("/api/restocking/recommendations", params={"budget": "inf"})
        assert response.status_code == 422

    # Order submission

    def test_create_order_returns_201_with_lead_time(self, client):
        """Test that a valid order is created with the expected totals and delivery date."""
        payload = {
            "budget": 1000,
            "lines": [
                {"sku": "WDG-001", "quantity": 2},   # 2 x 12.50 = 25.00, 10 days
                {"sku": "SNR-420", "quantity": 1},   # 1 x 27.50 = 27.50, 5 days
            ],
        }
        response = client.post("/api/restocking/orders", json=payload)
        assert response.status_code == 201

        order = response.json()
        assert order["status"] == "Submitted"
        assert order["total_cost"] == 52.5
        assert order["lead_time_days"] == 10

        submitted = datetime.fromisoformat(order["submitted_at"]).date()
        expected = datetime.fromisoformat(order["expected_delivery_date"]).date()
        assert expected == submitted + timedelta(days=10)

    def test_create_order_lead_time_is_slowest_line(self, client):
        """Test that the order lead time is the longest line lead time, since the order ships together."""
        payload = {
            "budget": 1000,
            "lines": [
                {"sku": "PSU-501", "quantity": 1},   # 5 days
                {"sku": "CTL-330", "quantity": 1},   # 21 days
            ],
        }
        order = client.post("/api/restocking/orders", json=payload).json()
        assert order["lead_time_days"] == 21

    def test_create_order_prices_come_from_catalog(self, client):
        """Test that the server ignores client-supplied prices and uses the catalog unit cost."""
        payload = {
            "budget": 1000,
            "lines": [{"sku": "WDG-001", "quantity": 1, "unit_cost": 0.01}],
        }
        order = client.post("/api/restocking/orders", json=payload).json()

        assert order["lines"][0]["unit_cost"] == 12.5
        assert order["total_cost"] == 12.5

    def test_create_order_rejects_over_budget(self, client):
        """Test that an order whose total exceeds the budget is rejected with 400."""
        payload = {"budget": 10, "lines": [{"sku": "WDG-001", "quantity": 1}]}
        response = client.post("/api/restocking/orders", json=payload)

        assert response.status_code == 400
        assert "exceeds budget" in response.json()["detail"]

    def test_create_order_rejects_unknown_sku(self, client):
        """Test that an order with a SKU not in the catalog is rejected with 400."""
        payload = {"budget": 1000, "lines": [{"sku": "NOPE-999", "quantity": 1}]}
        response = client.post("/api/restocking/orders", json=payload)

        assert response.status_code == 400
        assert "Unknown SKU" in response.json()["detail"]

    def test_create_order_rejects_duplicate_sku(self, client):
        """Test that the same SKU listed twice is rejected with 400."""
        payload = {
            "budget": 1000,
            "lines": [{"sku": "WDG-001", "quantity": 1}, {"sku": "WDG-001", "quantity": 2}],
        }
        response = client.post("/api/restocking/orders", json=payload)

        assert response.status_code == 400
        assert "more than once" in response.json()["detail"]

    def test_create_order_rejects_zero_quantity(self, client):
        """Test that a line with quantity 0 fails validation."""
        payload = {"budget": 1000, "lines": [{"sku": "WDG-001", "quantity": 0}]}
        response = client.post("/api/restocking/orders", json=payload)
        assert response.status_code == 422

    def test_create_order_rejects_infinite_budget(self, client):
        """Test that an order with an infinite budget fails validation."""
        payload = {"budget": "inf", "lines": [{"sku": "WDG-001", "quantity": 1}]}
        response = client.post("/api/restocking/orders", json=payload)
        assert response.status_code == 422

    def test_create_order_rejects_empty_lines(self, client):
        """Test that an order with no lines fails validation."""
        response = client.post("/api/restocking/orders", json={"budget": 1000, "lines": []})
        assert response.status_code == 422

    def test_submitted_order_appears_in_list(self, client):
        """Test that a submitted order can be read back from the list endpoint."""
        created = client.post(
            "/api/restocking/orders",
            json={"budget": 1000, "lines": [{"sku": "GSK-203", "quantity": 3}]},
        ).json()

        orders = client.get("/api/restocking/orders").json()
        assert any(order["id"] == created["id"] for order in orders)

    def test_submitted_orders_newest_first(self, client):
        """Test that the list is ordered newest first."""
        first = client.post(
            "/api/restocking/orders",
            json={"budget": 1000, "lines": [{"sku": "GSK-203", "quantity": 1}]},
        ).json()
        second = client.post(
            "/api/restocking/orders",
            json={"budget": 1000, "lines": [{"sku": "GSK-203", "quantity": 2}]},
        ).json()

        orders = client.get("/api/restocking/orders").json()
        assert orders[0]["id"] == second["id"]
        assert orders[1]["id"] == first["id"]

    def test_open_restock_orders_reduce_recommendation(self, client):
        """Test that units already on a submitted restock order are not recommended again."""
        before = client.get("/api/restocking/recommendations", params={"budget": 100000}).json()
        wdg_before = next(item for item in before["items"] if item["sku"] == "WDG-001")

        client.post(
            "/api/restocking/orders",
            json={"budget": 1000, "lines": [{"sku": "WDG-001", "quantity": 2}]},
        )

        after = client.get("/api/restocking/recommendations", params={"budget": 100000}).json()
        wdg_after = next(item for item in after["items"] if item["sku"] == "WDG-001")

        assert wdg_after["on_order"] == 2
        assert wdg_after["recommended_quantity"] == wdg_before["recommended_quantity"] - 2

    def test_restock_orders_do_not_change_customer_orders(self, client):
        """Test that submitting a restock order leaves the customer orders and dashboard totals unchanged."""
        orders_before = client.get("/api/orders").json()
        dashboard_before = client.get("/api/dashboard/summary").json()

        client.post(
            "/api/restocking/orders",
            json={"budget": 1000, "lines": [{"sku": "WDG-001", "quantity": 1}]},
        )

        orders_after = client.get("/api/orders").json()
        dashboard_after = client.get("/api/dashboard/summary").json()

        assert len(orders_after) == len(orders_before) == 250
        assert dashboard_after == dashboard_before

    def test_restock_orders_start_empty_in_each_test(self, client):
        """Test that the per-test fixture gives each test a clean restock order list."""
        assert client.get("/api/restocking/orders").json() == []
