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