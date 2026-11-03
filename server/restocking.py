"""
Restocking recommendations and order submission.

Recommendations are derived from the demand forecast, the inventory levels, and the
restock catalog (unit cost and supplier lead time). Submitted restock orders are
appended to mock_data.restock_orders. That list is separate from the customer
orders list, so restock orders never change /api/orders or dashboard totals.
"""
import math
import threading
from datetime import datetime, timedelta

from mock_data import demand_forecasts, inventory_items, restock_catalog, restock_orders

# The POST endpoint runs in FastAPI's threadpool, so two orders can be numbered at once.
# This lock covers numbering and appending together so ids stay unique
_order_lock = threading.Lock()


def _catalog_by_sku() -> dict:
    return {entry["sku"]: entry for entry in restock_catalog}


def _inventory_by_sku() -> dict:
    # SKUs are unique across inventory.json, so keying by SKU is safe
    return {item["sku"]: item for item in inventory_items}


def _open_restock_units() -> dict:
    """Units already on submitted restock orders, keyed by SKU.

    Every restock order is final once submitted (there is no fulfilment step yet),
    so all of them count as open. This is subtracted from the need below so that
    repeated submissions do not order the same demand twice.
    """
    open_units = {}
    for order in restock_orders:
        for line in order["lines"]:
            open_units[line["sku"]] = open_units.get(line["sku"], 0) + line["quantity"]
    return open_units


def _candidates() -> list:
    """Forecast items that need restocking, in priority order (before the budget is applied)."""
    catalog = _catalog_by_sku()
    inventory = _inventory_by_sku()
    open_units = _open_restock_units()

    candidates = []
    for forecast in demand_forecasts:
        sku = forecast["item_sku"]

        # Without a catalog entry there is no unit cost or supplier, so the item cannot be ordered
        entry = catalog.get(sku)
        if entry is None:
            continue

        # Only rising demand is restocked. Falling demand (e.g. MTR-304) is left alone
        if forecast["forecasted_demand"] <= forecast["current_demand"]:
            continue

        # Forecast SKUs without an inventory row are treated as zero stock and flagged in the UI
        stock = inventory.get(sku)
        on_hand = stock["quantity_on_hand"] if stock else 0
        on_order = open_units.get(sku, 0)
        need = max(0, forecast["forecasted_demand"] - on_hand - on_order)
        if need == 0:
            continue

        low_stock = stock is not None and on_hand <= stock["reorder_point"]
        # max(..., 1) guards against a zero current demand
        growth = forecast["forecasted_demand"] / max(forecast["current_demand"], 1)

        candidates.append({
            "sku": sku,
            "item_name": entry["item_name"],
            "supplier_name": entry["supplier_name"],
            "unit_cost": entry["unit_cost"],
            "on_hand": on_hand,
            "on_order": on_order,
            "has_stock_record": stock is not None,
            "current_demand": forecast["current_demand"],
            "forecasted_demand": forecast["forecasted_demand"],
            "trend": forecast["trend"],
            "need": need,
            "lead_time_days": entry["lead_time_days"],
            "_low_stock": low_stock,
            "_growth": growth,
        })

    # Items already at or below their reorder point come first. Within each group,
    # the fastest-growing demand comes first. False sorts before True, so negate the flag
    candidates.sort(key=lambda c: (not c["_low_stock"], -c["_growth"]))
    return candidates


def build_recommendations(budget: float) -> dict:
    candidates = _candidates()

    # max_budget is the cost of covering every candidate in full. It does not depend on
    # the requested budget, so the frontend can size its slider from any response
    max_budget = round(sum(c["need"] * c["unit_cost"] for c in candidates), 2)

    items = []
    spent = 0.0
    for c in candidates:
        remaining = budget - spent
        full_cost = c["need"] * c["unit_cost"]

        # Compare in cents, matching how line totals are rounded
        if round(full_cost, 2) <= round(remaining, 2):
            quantity = c["need"]
        else:
            # Partial fill: buy as many units as the remaining budget covers, so the
            # budget is used up rather than leaving money unspent on a skipped item
            # Rounding the quotient first stops float error from dropping a unit (e.g. 2.9999999 -> 3)
            quantity = math.floor(round(remaining / c["unit_cost"], 9))
            # Float division can land one unit too high when the budget divides evenly.
            # Step back until the rounded cost fits in the remaining budget
            while quantity > 0 and round(quantity * c["unit_cost"], 2) > round(remaining, 2):
                quantity -= 1

        if quantity <= 0:
            continue

        line_total = round(quantity * c["unit_cost"], 2)
        spent = round(spent + line_total, 2)
        items.append({
            "sku": c["sku"],
            "item_name": c["item_name"],
            "supplier_name": c["supplier_name"],
            "unit_cost": c["unit_cost"],
            "on_hand": c["on_hand"],
            "on_order": c["on_order"],
            "has_stock_record": c["has_stock_record"],
            "current_demand": c["current_demand"],
            "forecasted_demand": c["forecasted_demand"],
            "trend": c["trend"],
            "recommended_quantity": quantity,
            "line_total": line_total,
            "lead_time_days": c["lead_time_days"],
        })

    return {
        "budget": budget,
        "max_budget": max_budget,
        "spent": spent,
        "remaining": round(budget - spent, 2),
        "items": items,
    }


def create_restock_order(budget: float, lines: list) -> dict:
    """Validate and store a restock order. Raises ValueError with a user-facing message on failure."""
    catalog = _catalog_by_sku()

    seen = set()
    priced_lines = []
    for line in lines:
        sku = line["sku"]
        if sku in seen:
            raise ValueError(f"SKU {sku} appears more than once in this order")
        seen.add(sku)

        entry = catalog.get(sku)
        if entry is None:
            raise ValueError(f"Unknown SKU: {sku}")

        # Unit cost and lead time always come from the catalog, never from the client
        quantity = line["quantity"]
        priced_lines.append({
            "sku": sku,
            "item_name": entry["item_name"],
            "supplier_name": entry["supplier_name"],
            "unit_cost": entry["unit_cost"],
            "quantity": quantity,
            "line_total": round(quantity * entry["unit_cost"], 2),
            "lead_time_days": entry["lead_time_days"],
        })

    total_cost = round(sum(line["line_total"] for line in priced_lines), 2)
    # Compare in cents, since the total is rounded the same way
    if total_cost > round(budget, 2):
        raise ValueError(f"Order total ${total_cost:,.2f} exceeds budget ${budget:,.2f}")

    # The order ships as one unit, so it is ready when its slowest supplier is ready
    lead_time_days = max(line["lead_time_days"] for line in priced_lines)
    submitted_at = datetime.now().replace(microsecond=0)
    expected_delivery = (submitted_at + timedelta(days=lead_time_days)).date()

    # Numbering is based on the list length. Restock orders are only ever appended,
    # so numbers stay unique for the life of the process
    with _order_lock:
        number = len(restock_orders) + 1
        order = {
            "id": f"restock-{number:04d}",
            "order_number": f"RST-{number:04d}",
            "status": "Submitted",
            "submitted_at": submitted_at.isoformat(),
            "lead_time_days": lead_time_days,
            "expected_delivery_date": expected_delivery.isoformat(),
            "total_cost": total_cost,
            "lines": priced_lines,
        }
        restock_orders.append(order)
    return order


def list_restock_orders() -> list:
    # Newest first. A reversed copy, so the stored list is not mutated
    return list(reversed(restock_orders))
