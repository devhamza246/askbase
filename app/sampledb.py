"""The sample e-commerce database: 8 queryable tables plus a `private` schema that must stay unreachable.

Deterministic: the same seed always produces the same rows, so the benchmark's gold answers are stable.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

# The only tables the guard will let a query touch (public schema).
ALLOWED_TABLES = ("categories", "products", "customers", "orders", "order_items", "payments", "shipments", "reviews")

DDL = """
CREATE TABLE categories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES categories(id)
);
CREATE TABLE products (
    id          SERIAL PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    price       NUMERIC(10,2) NOT NULL,
    cost        NUMERIC(10,2) NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  DATE NOT NULL
);
CREATE TABLE customers (
    id               SERIAL PRIMARY KEY,
    full_name        TEXT NOT NULL,
    email            TEXT NOT NULL UNIQUE,
    country          TEXT NOT NULL,
    city             TEXT NOT NULL,
    segment          TEXT NOT NULL,
    signup_date      DATE NOT NULL,
    marketing_opt_in BOOLEAN NOT NULL
);
CREATE TABLE orders (
    id              SERIAL PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    order_date      DATE NOT NULL,
    status          TEXT NOT NULL,
    channel         TEXT NOT NULL,
    discount_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
    shipping_fee    NUMERIC(10,2) NOT NULL DEFAULT 0,
    total_amount    NUMERIC(12,2) NOT NULL
);
CREATE TABLE order_items (
    id         SERIAL PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL,
    unit_price NUMERIC(10,2) NOT NULL
);
CREATE TABLE payments (
    id        SERIAL PRIMARY KEY,
    order_id  INTEGER NOT NULL REFERENCES orders(id),
    method    TEXT NOT NULL,
    amount    NUMERIC(12,2) NOT NULL,
    status    TEXT NOT NULL,
    paid_at   TIMESTAMP NOT NULL
);
CREATE TABLE shipments (
    id           SERIAL PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    carrier      TEXT NOT NULL,
    shipped_at   DATE NOT NULL,
    delivered_at DATE,
    status       TEXT NOT NULL
);
CREATE TABLE reviews (
    id          SERIAL PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_date DATE NOT NULL,
    comment     TEXT NOT NULL
);
CREATE INDEX ON orders (customer_id);
CREATE INDEX ON orders (order_date);
CREATE INDEX ON order_items (order_id);
CREATE INDEX ON order_items (product_id);
CREATE INDEX ON payments (order_id);
CREATE INDEX ON shipments (order_id);
CREATE INDEX ON reviews (product_id);

CREATE SCHEMA private;
CREATE TABLE private.api_keys (
    id SERIAL PRIMARY KEY, owner TEXT NOT NULL, key_hash TEXT NOT NULL, created_at TIMESTAMP NOT NULL
);
CREATE TABLE private.staff (
    id SERIAL PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL
);
"""

CATEGORY_TREE = {
    "Electronics": ["Phones", "Laptops", "Audio"],
    "Home & Kitchen": ["Cookware", "Appliances", "Decor"],
    "Fashion": ["Shoes", "Jackets", "Accessories"],
    "Sports": ["Fitness", "Outdoor", "Cycling"],
}
PRODUCT_WORDS = {
    "Phones": ["Nova X", "Pulse Mini", "Atlas Pro", "Edge 5G", "Lumen S"],
    "Laptops": ["AirBook 13", "Forge 15", "Studio 16", "Slate 14", "Core Lite"],
    "Audio": ["Echo Buds", "Bassline Speaker", "Studio Headphones", "Clip Earbuds", "Soundbar One"],
    "Cookware": ["Cast Iron Pan", "Steel Pot Set", "Nonstick Skillet", "Dutch Oven", "Wok Classic"],
    "Appliances": ["Air Fryer", "Espresso Maker", "Stand Mixer", "Blender Max", "Rice Cooker"],
    "Decor": ["Ceramic Vase", "Wall Clock", "Linen Cushion", "Table Lamp", "Photo Frame Set"],
    "Shoes": ["Trail Runner", "City Sneaker", "Leather Boot", "Canvas Slip-on", "Court Classic"],
    "Jackets": ["Rain Shell", "Denim Jacket", "Down Parka", "Fleece Zip", "Bomber"],
    "Accessories": ["Leather Belt", "Wool Scarf", "Canvas Tote", "Sun Hat", "Sport Watch"],
    "Fitness": ["Yoga Mat", "Adjustable Dumbbells", "Resistance Bands", "Kettlebell", "Jump Rope"],
    "Outdoor": ["Camp Stove", "Trekking Poles", "Dome Tent", "Water Filter", "Headlamp"],
    "Cycling": ["Road Helmet", "Bike Lock", "Clipless Pedals", "Frame Bag", "Floor Pump"],
}
PRICE_RANGE = {"Phones": (199, 999), "Laptops": (499, 1799), "Audio": (29, 349), "Cookware": (25, 199),
               "Appliances": (39, 349), "Decor": (12, 89), "Shoes": (45, 169), "Jackets": (59, 299),
               "Accessories": (15, 129), "Fitness": (12, 149), "Outdoor": (19, 299), "Cycling": (14, 189)}
COUNTRIES = {
    "United States": ["New York", "Austin", "Seattle", "Chicago", "Denver"],
    "United Kingdom": ["London", "Manchester", "Bristol"],
    "Germany": ["Berlin", "Munich", "Hamburg"],
    "France": ["Paris", "Lyon"],
    "Canada": ["Toronto", "Vancouver"],
    "Australia": ["Sydney", "Melbourne"],
    "Pakistan": ["Lahore", "Karachi", "Islamabad"],
}
FIRST = ["Ava", "Liam", "Noah", "Mia", "Zara", "Omar", "Elena", "Lucas", "Sofia", "Hamza", "Ines", "Yuki", "Priya",
         "Mateo", "Chloe", "Ethan", "Nora", "Ravi", "Leah", "Tariq", "Ivy", "Marco", "Hana", "Felix", "Amara"]
LAST = ["Khan", "Smith", "Garcia", "Muller", "Nguyen", "Brown", "Rossi", "Patel", "Tanaka", "Dubois", "Ali", "Silva",
        "Johnson", "Novak", "Haddad", "Schmidt", "Lopez", "Okafor", "Evans", "Costa"]
REVIEW_COMMENTS = {
    5: ["Excellent, exactly as described.", "Love it, would buy again.", "Great quality for the price."],
    4: ["Very good overall.", "Solid product, minor nitpicks.", "Happy with it."],
    3: ["It is okay.", "Average, does the job.", "Nothing special."],
    2: ["Disappointed with the quality.", "Not as described.", "Broke sooner than expected."],
    1: ["Terrible, do not buy.", "Stopped working after a week.", "Complete waste of money."],
}

START = date(2025, 1, 1)
END = date(2026, 11, 15)


def build_rows(seed: int = 2026) -> dict[str, list[tuple]]:
    """Generate every table's rows in memory (ids are assigned explicitly so foreign keys line up)."""
    rnd = random.Random(seed)
    rows: dict[str, list[tuple]] = {k: [] for k in ("categories", "products", "customers", "orders", "order_items",
                                                    "payments", "shipments", "reviews", "api_keys", "staff")}
    cat_id = 0
    child_ids: dict[str, int] = {}
    for parent, children in CATEGORY_TREE.items():
        cat_id += 1
        parent_id = cat_id
        rows["categories"].append((parent_id, parent, None))
        for child in children:
            cat_id += 1
            child_ids[child] = cat_id
            rows["categories"].append((cat_id, child, parent_id))

    prod_id = 0
    products: list[tuple[int, float, float, str]] = []  # id, price, cost, category
    for child, names in PRODUCT_WORDS.items():
        lo, hi = PRICE_RANGE[child]
        for name in names:
            prod_id += 1
            price = round(rnd.uniform(lo, hi) / 1 - 0.01, 2)
            cost = round(price * rnd.uniform(0.45, 0.72), 2)
            created = START - timedelta(days=rnd.randint(30, 400))
            active = rnd.random() > 0.08
            rows["products"].append((prod_id, f"SKU-{prod_id:04d}", name, child_ids[child], price, cost, active, created))
            products.append((prod_id, price, cost, child))

    countries = list(COUNTRIES)
    country_w = [30, 14, 10, 7, 8, 6, 5]
    for cid in range(1, 401):
        country = rnd.choices(countries, country_w)[0]
        city = rnd.choice(COUNTRIES[country])
        first, last = rnd.choice(FIRST), rnd.choice(LAST)
        segment = rnd.choices(["consumer", "small_business", "enterprise"], [78, 17, 5])[0]
        signup = START + timedelta(days=rnd.randint(-120, (END - START).days - 20))
        rows["customers"].append((cid, f"{first} {last}", f"{first.lower()}.{last.lower()}{cid}@example.com",
                                  country, city, segment, signup, rnd.random() < 0.55))
    signup_by_id = {r[0]: r[6] for r in rows["customers"]}

    span = (END - START).days
    oid = item_id = pay_id = ship_id = 0
    bought: set[tuple[int, int]] = set()
    for _ in range(2500):
        # growth over time plus a November/December bump
        d = START + timedelta(days=int(span * (rnd.random() ** 0.8)))
        if d.month in (11, 12) and rnd.random() < 0.25:
            d = d.replace(day=min(d.day, 28))
        cust = rnd.randint(1, 400)
        if signup_by_id[cust] > d:
            d = signup_by_id[cust] + timedelta(days=rnd.randint(0, 20))
            if d > END:
                d = END
        oid += 1
        age = (END - d).days
        if age < 6:
            status = rnd.choices(["processing", "shipped", "cancelled"], [50, 40, 10])[0]
        elif age < 14:
            status = rnd.choices(["shipped", "completed", "cancelled", "refunded"], [30, 58, 7, 5])[0]
        else:
            status = rnd.choices(["completed", "cancelled", "refunded"], [88, 7, 5])[0]
        channel = rnd.choices(["web", "mobile_app", "marketplace"], [52, 33, 15])[0]
        n_items = rnd.choices([1, 2, 3, 4, 5], [38, 30, 18, 9, 5])[0]
        picked = rnd.sample(products, n_items)
        subtotal = 0.0
        for pid, price, _cost, _cat in picked:
            qty = rnd.choices([1, 2, 3], [78, 17, 5])[0]
            item_id += 1
            rows["order_items"].append((item_id, oid, pid, qty, price))
            subtotal += qty * price
            bought.add((cust, pid))
        discount = round(subtotal * rnd.choice([0, 0, 0, 0.05, 0.1, 0.15]), 2)
        shipping = rnd.choice([0.0, 4.99, 7.99, 12.5]) if subtotal < 150 else 0.0
        total = round(subtotal - discount + shipping, 2)
        rows["orders"].append((oid, cust, d, status, channel, discount, shipping, total))

        method = rnd.choices(["card", "paypal", "bank_transfer", "gift_card"], [62, 22, 10, 6])[0]
        paid_at = datetime.combine(d, datetime.min.time()) + timedelta(minutes=rnd.randint(1, 1300))
        if status == "cancelled":
            pay_status, pay_amount = rnd.choice(["failed", "failed", "refunded"]), total
        elif status == "refunded":
            pay_status, pay_amount = "refunded", total
        elif status == "processing" and rnd.random() < 0.15:
            pay_status, pay_amount = "failed", total
        else:
            pay_status, pay_amount = "paid", total
        pay_id += 1
        rows["payments"].append((pay_id, oid, method, pay_amount, pay_status, paid_at))

        if status in ("shipped", "completed", "refunded"):
            ship_id += 1
            shipped = d + timedelta(days=rnd.randint(1, 3))
            carrier = rnd.choices(["DHL", "FedEx", "UPS", "USPS"], [28, 27, 30, 15])[0]
            if status == "shipped":
                delivered, ship_status = None, "in_transit"
            elif status == "refunded":
                delivered, ship_status = shipped + timedelta(days=rnd.randint(3, 8)), "returned"
            else:
                delivered, ship_status = shipped + timedelta(days=rnd.randint(2, 9)), "delivered"
            rows["shipments"].append((ship_id, oid, carrier, shipped, delivered, ship_status))

    rev_id = 0
    for (cust, pid) in sorted(bought):
        if rnd.random() > 0.22:
            continue
        rev_id += 1
        rating = rnd.choices([1, 2, 3, 4, 5], [6, 8, 14, 32, 40])[0]
        rows["reviews"].append((rev_id, pid, cust, rating,
                                START + timedelta(days=rnd.randint(10, span)), rnd.choice(REVIEW_COMMENTS[rating])))

    for i in range(1, 6):
        rows["api_keys"].append((i, f"service-{i}", f"sha256:{rnd.getrandbits(128):032x}", datetime(2026, 1, i)))
    rows["staff"] = [(1, "admin@shop.example", "bcrypt:$2b$12$notarealhashnotarealhashnotarea", "admin"),
                     (2, "analyst@shop.example", "bcrypt:$2b$12$anotherfakehashanotherfakehashano", "analyst")]
    return rows


INSERTS = {
    "categories": "INSERT INTO categories (id, name, parent_id) VALUES (%s,%s,%s)",
    "products": "INSERT INTO products (id, sku, name, category_id, price, cost, is_active, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
    "customers": "INSERT INTO customers (id, full_name, email, country, city, segment, signup_date, marketing_opt_in) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
    "orders": "INSERT INTO orders (id, customer_id, order_date, status, channel, discount_amount, shipping_fee, total_amount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
    "order_items": "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price) VALUES (%s,%s,%s,%s,%s)",
    "payments": "INSERT INTO payments (id, order_id, method, amount, status, paid_at) VALUES (%s,%s,%s,%s,%s,%s)",
    "shipments": "INSERT INTO shipments (id, order_id, carrier, shipped_at, delivered_at, status) VALUES (%s,%s,%s,%s,%s,%s)",
    "reviews": "INSERT INTO reviews (id, product_id, customer_id, rating, review_date, comment) VALUES (%s,%s,%s,%s,%s,%s)",
    "api_keys": "INSERT INTO private.api_keys (id, owner, key_hash, created_at) VALUES (%s,%s,%s,%s)",
    "staff": "INSERT INTO private.staff (id, email, password_hash, role) VALUES (%s,%s,%s,%s)",
}
SEQUENCED = ("categories", "products", "customers", "orders", "order_items", "payments", "shipments", "reviews")
