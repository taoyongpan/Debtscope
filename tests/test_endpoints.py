"""HTTP entrypoint discovery: Flask blueprints, FastAPI routers, generic @route."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from debtscope.core.endpoints import discover_endpoints  # noqa: E402
from debtscope.core.python_indexer import PythonIndexer  # noqa: E402


def index_code(files: dict):
    tmp = tempfile.mkdtemp()
    for rel, src in files.items():
        path = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
    return PythonIndexer().index(tmp)


def map_eps(eps):
    return {(e.method, e.path): e for e in eps}


class EndpointDiscoveryTest(unittest.TestCase):
    def test_flask_blueprint_double_prefix(self):
        src = """
from flask import Flask, Blueprint
app = Flask(__name__)
bp = Blueprint('orders', __name__, url_prefix='/api/orders')

@app.route('/healthz')
def healthz():
    return 'ok'

@bp.get('/list')
def list_orders():
    return []

@bp.route('/multi', methods=['POST', 'PUT'])
def multi():
    return ''

app.register_blueprint(bp, url_prefix='/v1')
"""
        eps = map_eps(discover_endpoints(index_code({"app.py": src})))
        self.assertIn(("GET", "/healthz"), eps)
        self.assertIn(("GET", "/v1/api/orders/list"), eps)
        self.assertIn(("POST/PUT", "/v1/api/orders/multi"), eps)
        for e in eps.values():
            self.assertEqual(e.framework, "flask")

    def test_flask_route_defaults_to_get(self):
        src = """
from flask import Flask
app = Flask(__name__)
@app.route('/x')
def x():
    pass
"""
        eps = discover_endpoints(index_code({"a.py": src}))
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].method, "GET")

    def test_fastapi_router_prefix_and_include(self):
        src = """
from fastapi import FastAPI, APIRouter
api = FastAPI()
r = APIRouter(prefix='/internal')

@r.post('/job')
def run_job():
    pass

@r.get('/stats')
def stats():
    return {}

api.include_router(r, prefix='/v1')
"""
        eps = map_eps(discover_endpoints(index_code({"api.py": src})))
        self.assertIn(("POST", "/v1/internal/job"), eps)
        self.assertIn(("GET", "/v1/internal/stats"), eps)
        for e in eps.values():
            self.assertEqual(e.framework, "fastapi")

    def test_generic_route_decorator_is_any(self):
        src = """
def route(path):
    def deco(fn):
        fn.route = path
        return fn
    return deco

@route('/health')
def health():
    return 1
"""
        eps = discover_endpoints(index_code({"a.py": src}))
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].method, "ANY")
        self.assertEqual(eps[0].path, "/health")
        self.assertEqual(eps[0].framework, "generic")

    def test_demo_project_endpoints(self):
        demo = os.path.join(ROOT, "fixtures", "demo_project")
        eps = discover_endpoints(PythonIndexer().index(demo))
        self.assertEqual(len(eps), 11)
        m = map_eps(eps)
        expected = {
            ("GET", "/healthz"),
            ("GET", "/v1/api/orders/list"),
            ("POST", "/v1/api/orders/create"),
            ("GET", "/v1/api/orders/detail/<int:oid>"),
            ("GET", "/v1/internal/stats"),
            ("POST", "/v1/internal/recalc"),
            ("ANY", "/health"),
            ("ANY", "/users"),
            ("ANY", "/users/save"),
            ("ANY", "/users/tag"),
            ("ANY", "/reports/monthly"),
        }
        self.assertEqual(set(m.keys()), expected)
        # every endpoint points at a real handler line
        for e in eps:
            self.assertGreater(e.handler_line, 0)
            self.assertTrue(e.handler_qualname)


if __name__ == "__main__":
    unittest.main()
