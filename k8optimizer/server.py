"""Minimal REST API (stdlib http.server) exposing the optimization engine.

Endpoints
---------
POST /recommend   -> run recommend + safety + plan for workloads in JSON body
GET  /health      -> liveness

The payload is a list of workload dicts (see README). Keeps the engine usable
as a headless service the operator (or a UI) can call without a live cluster.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .api import Api, ApiError
from .cost import CostConfig, report_workload
from .models import QoSClass, UsageSample, Workload
from .recommender import RecommenderConfig, recommend
from .resize import PatchAction, build_plan
from .safety import SafetyConfig, approve, effective_recommendation
from .store import Store

#: HTML dashboard asset, served at GET /.
_DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


def workload_from_dict(d: dict) -> Workload:
    usage = [UsageSample(cpu_millis=s["cpu_millis"], memory_mib=s["memory_mib"])
             for s in d.get("usage", [])]
    qos = QoSClass(d.get("qos_class", "Burstable"))
    return Workload(
        name=d["name"], namespace=d.get("namespace", "default"),
        kind=d.get("kind", "Deployment"), replicas=int(d.get("replicas", 1)),
        cpu_request_millis=float(d.get("cpu_request_millis", 0)),
        cpu_limit_millis=d.get("cpu_limit_millis"),
        memory_request_mib=float(d.get("memory_request_mib", 0)),
        memory_limit_mib=d.get("memory_limit_mib"),
        qos_class=qos, usage=usage,
        requests_per_sec=float(d.get("requests_per_sec", 0)),
        capacity_per_replica_rps=d.get("capacity_per_replica_rps"),
        min_replicas=int(d.get("min_replicas", 1)),
        max_replicas=int(d.get("max_replicas", 100)),
        allow_in_place_resize=bool(d.get("allow_in_place_resize", True)),
    )


def evaluate(workloads: List[Workload]) -> dict:
    results, reports = [], []
    for w in workloads:
        rec = recommend(w)
        dec = approve(rec)
        eff = effective_recommendation(rec, dec)
        plan = build_plan(eff, dec)
        rep = report_workload(w, eff)
        reports.append(rep)
        results.append({
            "workload": f"{w.namespace}/{w.name}",
            "confidence": rec.confidence,
            "cpu": {"current": rec.cpu.current, "recommended": eff.cpu.recommended},
            "memory": {"current": rec.memory.current, "recommended": eff.memory.recommended},
            "replicas": rec.replicas.recommended if rec.replicas else None,
            "safety": {"allowed": dec.allowed, "strategy": dec.strategy, "reason": dec.reason},
            "actions": [a.__dict__ for a in plan],
            "cost": rep.to_dict(),
        })
    from .cost import summarize
    return {"results": results, "summary": summarize(reports)}


class Handler(BaseHTTPRequestHandler):
    api: Optional[Api] = None

    def _send(self, code: int, payload) -> None:
        if isinstance(payload, dict):
            body = json.dumps(payload).encode("utf-8")
            ctype = "application/json"
        else:
            body = payload
            ctype = "text/html"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _run(self, fn, *args) -> None:
        try:
            self._send(200, fn(*args))
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    # -- routes -----------------------------------------------------------
    def _route_api(self) -> bool:
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        hdrs = {k: v for k, v in self.headers.items()}
        api = self.api
        if api is None:
            return False

        if self.command == "POST" and path == "/api/login":
            self._run(api.login, self._read_body())
        elif self.command == "GET" and path == "/api/me":
            self._run(api.me, hdrs)
        elif self.command == "GET" and path == "/api/metrics":
            self._run(api.metrics, hdrs)
        elif self.command == "GET" and path.startswith("/api/logs/"):
            kind = path.rsplit("/", 1)[-1]
            limit = int(q.get("limit", ["100"])[0])
            self._run(api.logs, hdrs, kind, limit)
        elif self.command == "POST" and path == "/api/logs/errors":
            self._run(api.log_error, hdrs, self._read_body())
        elif self.command == "GET" and path == "/api/rca":
            self._run(api.rca, hdrs)
        elif self.command == "GET" and path == "/api/providers":
            self._run(api.providers, hdrs)
        elif self.command == "GET" and path == "/api/users":
            self._run(api.list_users, hdrs)
        elif self.command == "POST" and path == "/api/users":
            self._run(api.create_user, hdrs, self._read_body())
        elif self.command == "PUT" and path.endswith("/role"):
            username = path.split("/")[3]
            self._run(api.set_role, hdrs, username, self._read_body().get("role", "viewer"))
        elif self.command == "PUT" and path.endswith("/permissions"):
            username = path.split("/")[3]
            self._run(api.set_permissions, hdrs, username,
                      self._read_body().get("permissions", []))
        else:
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "version": __version__})
        elif self.path == "/" or self.path == "/index.html":
            self._send(200, _DASHBOARD_PATH.read_bytes())
        elif self._route_api():
            pass
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/recommend":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
                workloads = [workload_from_dict(d) for d in data.get("workloads", [])]
                self._send(200, evaluate(workloads))
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"error": str(exc)})
        elif self._route_api():
            pass
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self) -> None:
        if self._route_api():
            pass
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args) -> None:
        pass


def serve(host: str = "127.0.0.1", port: int = 8080,
          store: Optional[Store] = None) -> None:
    store = store or Store("k8optimizer.db")
    Handler.api = Api(store)
    httpd = HTTPServer((host, port), Handler)
    print(f"K8-Optimizer API v{__version__} listening on http://{host}:{port}")
    httpd.serve_forever()