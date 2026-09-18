"""Demo HTTP application: Flask blueprint + FastAPI router styles.

Debtscope parses this statically: the third-party packages don't need to be
installed. Every decorated handler becomes a monitored endpoint whose call
chain is tracked into the service/DAO layers below.
"""

from flask import Blueprint, Flask, request
from fastapi import APIRouter, FastAPI

import order_service

app = Flask(__name__)
bp = Blueprint("orders", __name__, url_prefix="/api/orders")

api = FastAPI(title="orders-internal")
router = APIRouter(prefix="/internal")


@bp.get("/list")
def list_orders():
    uid = request.args.get("uid")
    return {"orders": order_service.list_user_orders(uid)}


@bp.post("/create")
def create_order_endpoint():
    payload = request.get_json()
    return {"ok": True, "order": order_service.create_order(payload)}


@bp.get("/detail/<int:oid>")
def order_detail(oid):
    return {"order": order_service.get_order_detail(oid)}


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


app.register_blueprint(bp, url_prefix="/v1")


@router.get("/stats")
def order_stats():
    return order_service.orders_dashboard()


@router.post("/recalc")
def recalc_orders():
    return {"totals": order_service.recalculate_all()}


api.include_router(router, prefix="/v1")
