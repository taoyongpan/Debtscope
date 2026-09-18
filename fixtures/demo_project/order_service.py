"""Order domain: service + DAO layers, used to demo call-chain tracking.

Third-party imports (requests) are never executed by Debtscope — the indexer
parses code statically, so the packages need not be installed.
"""

import requests

from user_service import save_user

INVENTORY_BASE = "http://inventory.internal/api"


def _connect():
    # demo stub: a real project would return a pooled DB connection
    return None


def _cursor():
    conn = _connect()
    return conn.cursor()


def _query_one(sql, args):
    cur = _cursor()
    cur.execute(sql, args)
    return cur.fetchone()


def enrich_order(order):
    resp = requests.get(INVENTORY_BASE + "/sku/" + str(order["sku"]))
    return {"order": order, "inventory": resp.json()}


def list_user_orders(uid):
    cur = _cursor()
    cur.execute("SELECT id, sku FROM orders WHERE uid=?", (uid,))
    rows = cur.fetchall()
    orders = []
    for row in rows:
        # N+1 anti-pattern: a DB query AND an HTTP call inside the loop
        cur.execute("SELECT detail FROM order_items WHERE oid=?", (row[0],))
        detail = cur.fetchone()
        stock = requests.get(INVENTORY_BASE + "/stock/" + str(row[0])).json()
        orders.append({"detail": detail, "stock": stock})
    return orders


def get_order_detail(oid):
    order = _query_one("SELECT * FROM orders WHERE id=?", (oid,))
    return enrich_order(order)


def create_order(payload):
    save_user(payload["uid"], payload.get("profile"))
    try:
        return _persist_order(payload)
    except:  # 下单失败不应被静默吞掉，至少要记录日志
        pass
    return None


def _persist_order(payload):
    cur = _cursor()
    cur.execute(
        "INSERT INTO orders(uid, sku, qty) VALUES(?,?,?)",
        (payload["uid"], payload["sku"], payload["qty"]),
    )
    cur.connection.commit()
    return payload.get("id")


def orders_dashboard():
    orders = list_user_orders(None)
    return {"orders": orders, "totals": recalculate_all(orders)}


def recalculate_all(rows=None):
    total = 0
    for row in rows or []:
        total += len(row)
    return {"lines": total}
