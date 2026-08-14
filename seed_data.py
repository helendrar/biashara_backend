"""
Biashara AI — Demo data seeder

Populates a user's account with ~6 weeks of realistic backdated activity for a
small Nairobi duka: daily sales, weekly restocking, recurring expenses, stock
movements, and repeat customers.

WHY THIS EXISTS
---------------
Clicking through the app to build demo data stamps everything with today's date,
which breaks the features that depend on history:

  • Cashflow forecast   — needs 5+ distinct days
  • Reorder suggestions — need 2+ sales per item, spread over time
  • Health score        — measures logging consistency across a date span

This writes proper backdated timestamps so those features actually work.

USAGE
-----
    python seed_data.py <YOUR_FIREBASE_UID>
    python seed_data.py <YOUR_FIREBASE_UID> --wipe     # clear existing data first

Find your UID in Firebase Console → Authentication → Users.
"""

import random
import sys
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import credentials, firestore

DAYS_OF_HISTORY = 45
SEED = 42  # fixed so demo runs are reproducible

COLLECTIONS = ["transactions", "inventory", "stockMovements", "customers", "advisorMessages"]

# ── Stock: (name, unit cost to shop, sale price, starting qty, low-stock threshold, popularity 1-5)
STOCK = [
    ("Maize flour", 160, 190, 40, 8, 5),
    ("Sugar", 145, 175, 35, 8, 5),
    ("Cooking oil", 280, 330, 20, 5, 4),
    ("Rice", 130, 160, 30, 6, 4),
    ("Bread", 60, 70, 15, 5, 5),
    ("Fresh milk", 55, 70, 24, 6, 5),
    ("Bar soap", 90, 110, 25, 5, 3),
    ("Washing powder", 120, 150, 18, 4, 3),
    ("Tea leaves", 95, 120, 20, 5, 4),
    ("Salt", 30, 45, 30, 6, 2),
    ("Matches", 8, 15, 50, 10, 2),
    ("Airtime card", 95, 100, 40, 10, 4),
    ("Soda", 45, 60, 48, 12, 4),
    ("Eggs", 15, 20, 60, 12, 3),
    ("Charcoal", 350, 420, 8, 2, 2),
]

# Items given an expiry date, in days from today (negative = already expired)
EXPIRY_DAYS = {
    "Fresh milk": 4,
    "Bread": 2,
    "Eggs": 11,
    "Maize flour": 120,
}

CUSTOMERS = [
    ("Mama Njeri", "0712345678", ["Maize flour and sugar", "Cooking oil", "Rice and beans", "Maize flour"]),
    ("John Kamau", "0723456789", ["Airtime and soda", "Bread and milk", "Airtime"]),
    ("Grace Wanjiku", "0734567890", ["Washing powder and soap", "Cooking oil and salt"]),
    ("Peter Otieno", "0745678901", ["Charcoal", "Charcoal and matches", "Charcoal"]),
    ("Auntie Fatuma", "0756789012", ["Rice, sugar and tea", "Maize flour", "Sugar and cooking oil", "Rice"]),
    ("David Mwangi", "0767890123", ["Bread, milk and eggs", "Soda"]),
    ("Sarah Achieng", "0778901234", ["Sanitary pads and soap", "Washing powder"]),
    ("Boniface Kiptoo", "0789012345", ["Maize flour, sugar, oil", "Tea leaves and sugar"]),
    ("Mercy Nduta", "0790123456", ["Eggs and bread", "Milk"]),
    ("Samuel Wafula", "0701234567", ["Airtime", "Soda and biscuits"]),
]

RECURRING_EXPENSES = [
    ("Shop rent", 8000, "monthly"),
    ("Electricity", 1200, "monthly"),
    ("Transport to market", 400, "weekly"),
    ("Water", 300, "weekly"),
    ("Licence fee", 2000, "once"),
]


def days_ago(n: int, hour: int = 9, minute: int = 0) -> datetime:
    base = datetime.now(timezone.utc) - timedelta(days=n)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def wipe(db, uid: str):
    print("Clearing existing data…")
    for name in COLLECTIONS:
        docs = list(db.collection(name).where("uid", "==", uid).stream())
        for d in docs:
            d.reference.delete()
        print(f"  removed {len(docs):>4} from {name}")


def seed(db, uid: str):
    rng = random.Random(SEED)
    batch_count = {"transactions": 0, "inventory": 0, "stockMovements": 0, "customers": 0}

    # ── Inventory + opening stock movements ──────────────────
    item_ids = {}
    for name, cost, price, qty, threshold, _pop in STOCK:
        doc = {
            "itemName": name,
            "quantity": qty,
            "lowStockThreshold": threshold,
            "unitPrice": price,
            "uid": uid,
            "createdAt": days_ago(DAYS_OF_HISTORY, 8),
        }
        if name in EXPIRY_DAYS:
            doc["expiresAt"] = datetime.now(timezone.utc) + timedelta(days=EXPIRY_DAYS[name])

        ref = db.collection("inventory").add(doc)[1]
        item_ids[name] = ref.id
        batch_count["inventory"] += 1

        db.collection("stockMovements").add({
            "itemId": ref.id,
            "itemName": name,
            "uid": uid,
            "quantityChange": qty,
            "reason": "initial",
            "createdAt": days_ago(DAYS_OF_HISTORY, 8),
        })
        batch_count["stockMovements"] += 1

    # ── Daily trading ────────────────────────────────────────
    stock_level = {name: qty for name, _c, _p, qty, _t, _pop in STOCK}

    for day in range(DAYS_OF_HISTORY - 1, -1, -1):
        date = days_ago(day)
        weekday = date.weekday()

        # Sundays are quiet; Fridays/Saturdays are busiest.
        if weekday == 6:
            sale_count = rng.randint(2, 4)
        elif weekday in (4, 5):
            sale_count = rng.randint(7, 11)
        else:
            sale_count = rng.randint(4, 8)

        weighted = [n for n, _c, _p, _q, _t, pop in STOCK for _ in range(pop)]

        for s in range(sale_count):
            item = rng.choice(weighted)
            _n, cost, price, _q, _t, _pop = next(x for x in STOCK if x[0] == item)
            units = rng.choice([1, 1, 1, 2, 2, 3])
            hour = rng.randint(7, 19)
            minute = rng.randint(0, 59)
            ts = days_ago(day, hour, minute)

            if stock_level[item] < units:
                continue

            db.collection("transactions").add({
                "amount": price * units,
                "note": f"Sold {units} × {item}",
                "type": "income",
                "category": "Sales",
                "uid": uid,
                "createdAt": ts,
            })
            batch_count["transactions"] += 1

            stock_level[item] -= units
            db.collection("stockMovements").add({
                "itemId": item_ids[item],
                "itemName": item,
                "uid": uid,
                "quantityChange": -units,
                "reason": "sale",
                "createdAt": ts,
            })
            batch_count["stockMovements"] += 1

        # Restock run every ~7 days, on the items that have run down most.
        if day % 7 == 3:
            low = sorted(stock_level.items(), key=lambda kv: kv[1])[:5]
            for item, _lvl in low:
                _n, cost, _p, _q, _t, _pop = next(x for x in STOCK if x[0] == item)
                units = rng.randint(10, 25)
                ts = days_ago(day, 7, 30)

                db.collection("transactions").add({
                    "amount": cost * units,
                    "note": f"Restocked {units} × {item}",
                    "type": "expense",
                    "category": "Stock / restock",
                    "uid": uid,
                    "createdAt": ts,
                })
                batch_count["transactions"] += 1

                stock_level[item] += units
                db.collection("stockMovements").add({
                    "itemId": item_ids[item],
                    "itemName": item,
                    "uid": uid,
                    "quantityChange": units,
                    "reason": "restock",
                    "createdAt": ts,
                })
                batch_count["stockMovements"] += 1

        # Recurring overheads
        expense_category = {
            "Shop rent": "Rent",
            "Electricity": "Utilities",
            "Water": "Utilities",
            "Transport to market": "Transport",
            "Licence fee": "Licence & fees",
        }
        for label, amount, cadence in RECURRING_EXPENSES:
            fire = (
                (cadence == "monthly" and date.day == 1)
                or (cadence == "weekly" and weekday == 0)
                or (cadence == "once" and day == DAYS_OF_HISTORY - 1)
            )
            if fire:
                db.collection("transactions").add({
                    "amount": amount,
                    "note": label,
                    "type": "expense",
                    "category": expense_category.get(label, "Other expense"),
                    "uid": uid,
                    "createdAt": days_ago(day, 8, 15),
                })
                batch_count["transactions"] += 1

    # Sync final stock levels back to the inventory docs.
    for name, item_id in item_ids.items():
        db.collection("inventory").document(item_id).update({"quantity": max(0, stock_level[name])})

    # ── Customers with repeat purchases ──────────────────────
    stock_names = {name for name, *_ in STOCK}
    for name, phone, purchases in CUSTOMERS:
        for i, note in enumerate(purchases):
            gap = DAYS_OF_HISTORY - (i + 1) * rng.randint(5, 11)
            when = days_ago(max(0, gap), rng.randint(9, 18), rng.randint(0, 59))

            # Single-item notes (no "and"/",") get linked to real stock, same as
            # the app's "From your stock" flow — feeds FR-13's top-products ranking.
            linked_item = None
            if " and " not in note and "," not in note:
                for stock_name in stock_names:
                    if stock_name.lower() in note.lower():
                        linked_item = stock_name
                        break

            qty = rng.randint(1, 3) if linked_item else None
            _n, cost, price, _q, _t, _pop = next(x for x in STOCK if x[0] == linked_item) if linked_item else (None,) * 6

            db.collection("customers").add({
                "customerName": name,
                "phone": phone,
                "purchaseNote": f"{qty} × {linked_item}" if linked_item else note,
                "purchaseAmount": (price * qty) if linked_item else rng.choice([180, 250, 320, 450, 600, 750, 900, 1200]),
                "itemId": item_ids.get(linked_item),
                "itemName": linked_item,
                "quantity": qty,
                "uid": uid,
                "lastPurchaseAt": when,
            })
            batch_count["customers"] += 1

    return batch_count


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    uid = sys.argv[1]
    should_wipe = "--wipe" in sys.argv

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate("firebase-key.json"))
    db = firestore.client()

    print(f"\nSeeding demo data for UID: {uid}")
    print(f"Period: last {DAYS_OF_HISTORY} days\n")

    if should_wipe:
        wipe(db, uid)
        print()

    counts = seed(db, uid)

    print("\n✅ Done. Created:")
    for k, v in counts.items():
        print(f"   {v:>4}  {k}")
    print("\nOpen the app — Insights, reorder suggestions and the health score")
    print("should all populate now.\n")


if __name__ == "__main__":
    main()
