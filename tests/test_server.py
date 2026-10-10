import json
from pathlib import Path

from k8optimizer.server import evaluate, workload_from_dict


def _load_examples():
    p = Path(__file__).parent.parent / "examples" / "workloads.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return [workload_from_dict(d) for d in data["workloads"]]


def test_evaluate_returns_summary_and_results():
    out = evaluate(_load_examples())
    assert out["summary"]["workloads"] == 2
    assert out["summary"]["savings_monthly"] > 0
    results = out["results"]
    assert len(results) == 2

    checkout = results[0]
    assert checkout["workload"] == "prod/checkout-svc"
    # over-provisioned: cpu recommended below current
    assert checkout["cpu"]["recommended"] < checkout["cpu"]["current"]
    assert checkout["safety"]["allowed"] is True
    assert checkout["safety"]["strategy"] == "in-place-resize"
    assert any(a["strategy"] == "in-place-resize" for a in checkout["actions"])

    legacy = results[1]
    assert legacy["workload"] == "batch/legacy-report"
    # Guaranteed stateful workload also uses in-place resize (no downtime)
    assert legacy["safety"]["strategy"] == "in-place-resize"


def test_http_server_routes():
    from http.server import HTTPServer
    from k8optimizer.server import Handler

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    server.server_close()

    import threading
    import urllib.request

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]

    def run():
        server.serve_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
            body = json.loads(resp.read())
            assert body["status"] == "ok"
    finally:
        server.shutdown()
        t.join(timeout=5)


def test_http_server_rbac_api():
    import threading
    import urllib.error
    import urllib.request
    from http.server import HTTPServer

    from k8optimizer.server import Handler, serve
    from k8optimizer.store import Store
    from k8optimizer.api import Api

    store = Store(seed_admin=False)
    store.create_user("admin", "admin", "admin")
    store.create_user("peon", "pw", "viewer")
    Handler.api = Api(store)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]

    def run():
        server.serve_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def post(path, payload, token=None):
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", "Bearer " + token)
        return urllib.request.urlopen(req)

    try:
        # login -> token
        with post("/api/login", {"username": "admin", "password": "admin"}) as r:
            token = json.loads(r.read())["token"]

        # authorized metrics
        req = urllib.request.Request(base + "/api/metrics",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["summary"]["actions_total"] == 0

        # viewer token cannot manage users
        with post("/api/login", {"username": "peon", "password": "pw"}) as r:
            peon = json.loads(r.read())["token"]
        req = urllib.request.Request(base + "/api/users",
                                     headers={"Authorization": "Bearer " + peon})
        try:
            urllib.request.urlopen(req)
            assert False, "expected 403"
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

        # dashboard served at /
        with urllib.request.urlopen(base + "/") as r:
            assert b"K8-Optimizer" in r.read()
    finally:
        server.shutdown()
        t.join(timeout=5)