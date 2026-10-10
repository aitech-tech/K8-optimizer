"""In-cluster controller — the piece that turns plans into *applied* changes.

This is the autonomous loop: watch marked workloads, read their real metrics,
run recommend -> safety -> autoscaling-builders, and **apply** the resulting
VPA/HPA objects and zero-downtime patches to the live cluster.

Design stays fully testable offline:

* ``KubeClient`` is a Protocol (duck-typed) — the real implementation talks to
  the Kubernetes API over the in-cluster HTTP endpoint; tests inject a
  ``FakeKubeClient`` that records every call. No third-party deps: the real
  client uses only ``urllib`` + the service-account token from disk.
* ``MetricsSource`` is a Protocol — the real one reads the Metrics API
  (``metrics.k8s.io/v1beta1`` PodMetrics) aggregated to a workload; tests
  inject samples directly.

The reconcile loop runs as::

    while True:
        report = reconcile(client, metrics)
        sleep(interval)
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol, runtime_checkable

from .autoscaling import build_autoscaling_bundle
from .models import QoSClass, Recommendation, UsageSample, Workload
from .recommender import RecommenderConfig, recommend
from .resize import PatchAction, build_plan
from .safety import SafetyConfig, approve, effective_recommendation

# Label that marks a workload as "managed by k8-optimizer".
MANAGED_LABEL = "k8optimizer.aitech.tech/managed"
TARGET_LABEL = "k8optimizer.aitech.tech/optimize"


@dataclass
class ReconcileResult:
    """Outcome of reconciling one workload."""

    workload: str
    namespace: str
    allowed: bool
    reason: str
    applied_actions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.allowed


@dataclass
class ReconcileReport:
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    results: List[ReconcileResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "results": [r.__dict__ for r in self.results],
        }


@runtime_checkable
class KubeClient(Protocol):
    """Duck-typed Kubernetes API surface used by the controller."""

    def list_target_workloads(self) -> List[dict]: ...

    def apply_object(self, obj: dict) -> None: ...

    def patch_object(self, api_version: str, kind: str,
                     namespace: str, name: str, patch: dict) -> None: ...

    def record_event(self, namespace: str, involved: dict,
                     reason: str, message: str) -> None: ...


@runtime_checkable
class MetricsSource(Protocol):
    """Provides UsageSamples for a workload over a lookback window."""

    def samples(self, namespace: str, name: str, window_seconds: int) -> List[UsageSample]: ...


def workload_from_target(t: dict) -> Workload:
    """Convert a K8s target dict (Deployment/StatefulSet/DaemonSet) to Workload."""
    spec = t.get("spec", {})
    template = spec.get("template", {}).get("spec", {})
    containers = template.get("containers", []) or spec.get("containers", [])
    container = containers[0] if containers else {}

    # Determine requests/limits from the first container.
    res = container.get("resources", {})
    req = res.get("requests", {}) or {}
    lim = res.get("limits", {}) or {}
    cpu_req = _parse_cpu(req.get("cpu", "0"))
    mem_req = _parse_mem(req.get("memory", "0"))
    cpu_lim = _parse_cpu(lim.get("cpu")) if lim.get("cpu") else None
    mem_lim = _parse_mem(lim.get("memory")) if lim.get("memory") else None

    replicas = int(spec.get("replicas", 1))
    annotations = t.get("metadata", {}).get("annotations", {}) or {}

    # QoS: Guaranteed iff every container has equal requests & limits for cpu+mem.
    qos = QoSClass.BURSTABLE
    if req.get("cpu") and lim.get("cpu") and req.get("cpu") == lim.get("cpu") \
            and req.get("memory") and lim.get("memory") == req.get("memory") == lim.get("memory"):
        qos = QoSClass.GUARANTEED

    return Workload(
        name=t["metadata"]["name"],
        namespace=t["metadata"].get("namespace", "default"),
        kind=t.get("kind", "Deployment"),
        replicas=replicas,
        cpu_request_millis=cpu_req,
        cpu_limit_millis=cpu_lim,
        memory_request_mib=mem_req,
        memory_limit_mib=mem_lim,
        qos_class=qos,
        min_replicas=int(annotations.get("k8optimizer.aitech.tech/min-replicas",
                         spec.get("minReplicas", 1))),
        max_replicas=int(annotations.get("k8optimizer.aitech.tech/max-replicas",
                         spec.get("maxReplicas", 100))),
        allow_in_place_resize=annotations.get(
            "k8optimizer.aitech.tech/allow-in-place", "true").lower() != "false",
    )


def _parse_cpu(q: str) -> float:
    """Parse a K8s CPU quantity to millicores."""
    q = str(q).strip()
    if not q:
        return 0.0
    if q.endswith("m"):
        return float(q[:-1])
    if q.endswith("n"):
        return float(q[:-1]) / 1e6
    if q.endswith("u"):
        return float(q[:-1]) / 1e3
    return float(q) * 1000.0


def _parse_mem(q: str) -> float:
    """Parse a K8s memory quantity to MiB."""
    q = str(q).strip()
    if not q:
        return 0.0
    units = {"Ki": 1 / 1024.0, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0 * 1024.0,
             "k": 1 / 1024.0, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024.0}
    for suffix, mult in units.items():
        if q.endswith(suffix):
            return float(q[:-len(suffix)]) * mult
    # Plain bytes.
    try:
        return float(q) / (1024.0 * 1024.0)
    except ValueError:
        return 0.0


@dataclass
class ControllerConfig:
    interval_seconds: float = 300.0
    window_seconds: int = 86400
    recommender: RecommenderConfig = field(default_factory=RecommenderConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    reconcile_vpa: bool = True
    reconcile_hpa: bool = True
    dry_run: bool = False


def reconcile(client: KubeClient, metrics: MetricsSource,
              cfg: Optional[ControllerConfig] = None) -> ReconcileReport:
    """One pass: observe -> recommend -> gate -> apply, for every target."""
    cfg = cfg or ControllerConfig()
    report = ReconcileReport()

    targets = client.list_target_workloads()
    for t in targets:
        w = workload_from_target(t)
        key = f"{w.namespace}/{w.name}"
        result = ReconcileResult(workload=w.name, namespace=w.namespace,
                                 allowed=False, reason="")

        # 1. Observe real usage.
        w.usage = metrics.samples(w.namespace, w.name, cfg.window_seconds)

        # 2. Recommend + gate.
        rec = recommend(w, cfg.recommender)
        dec = approve(rec, cfg.safety)
        eff = effective_recommendation(rec, dec)
        result.allowed = dec.allowed
        result.reason = dec.reason

        if not dec.allowed:
            report.results.append(result)
            continue

        # 3. Build zero-downtime patch plan + autoscaling objects.
        plan = build_plan(eff, dec)
        bundle = build_autoscaling_bundle(
            w, eff, vpa=cfg.reconcile_vpa, hpa=cfg.reconcile_hpa)

        # 4. Apply (unless dry-run).
        if cfg.dry_run:
            result.applied_actions = [a.description for a in plan]
            if bundle.get("vpa"):
                result.applied_actions.append("create/update VPA")
            if bundle.get("hpa"):
                result.applied_actions.append("create/update HPA")
            report.results.append(result)
            continue

        try:
            for a in plan:
                client.patch_object(a.api_version, a.kind, a.namespace,
                                    a.name, a.patch)
                result.applied_actions.append(a.description)
            for obj in bundle.values():
                if obj:
                    client.apply_object(obj)
                    result.applied_actions.append(
                        f"{obj['kind']} -> {obj['metadata']['namespace']}/{obj['metadata']['name']}")
            client.record_event(
                w.namespace, {"kind": w.kind, "name": w.name},
                "Optimized", f"k8-optimizer applied {len(plan)} action(s) "
                             f"({dec.strategy})")
        except Exception as exc:  # noqa: BLE001
            result.errors.append(str(exc))

        report.results.append(result)

    return report


class FakeKubeClient:
    """In-memory KubeClient for tests & offline demos."""

    def __init__(self, targets: Optional[List[dict]] = None):
        self.targets = targets or []
        self.applied: List[dict] = []
        self.patched: List[dict] = []
        self.events: List[dict] = []
        self.fail_next_patch: int = 0  # force N patch failures for retry tests

    def list_target_workloads(self) -> List[dict]:
        return list(self.targets)

    def apply_object(self, obj: dict) -> None:
        self.applied.append(obj)

    def patch_object(self, api_version, kind, namespace, name, patch) -> None:
        if self.fail_next_patch > 0:
            self.fail_next_patch -= 1
            raise RuntimeError("simulated API failure")
        self.patched.append({"api_version": api_version, "kind": kind,
                             "namespace": namespace, "name": name,
                             "patch": patch})

    def record_event(self, namespace, involved, reason, message) -> None:
        self.events.append({"namespace": namespace, "involved": involved,
                            "reason": reason, "message": message})


class FakeMetrics:
    """MetricsSource returning fixed samples per workload."""

    def __init__(self, samples_by_key: Optional[Dict[str, List[UsageSample]]] = None):
        self.samples_by_key = samples_by_key or {}

    def samples(self, namespace, name, window_seconds) -> List[UsageSample]:
        return self.samples_by_key.get(f"{namespace}/{name}", [])


class HttpKubeClient:
    """Real KubeClient over the in-cluster API (or kubeconfig-ish env)."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None,
                 verify: bool = True):
        self.base_url = (base_url or os.environ.get(
            "KUBERNETES_SERVICE_HOST")).rstrip("/")
        self.token = token or self._load_token()
        self.verify = verify

    @staticmethod
    def _load_token() -> str:
        # In-cluster service account token.
        path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return os.environ.get("K8OPTIMIZER_TOKEN", "")

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if not self.base_url:
            raise RuntimeError("no Kubernetes API endpoint configured")
        url = f"https://{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req) as resp:  # noqa
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"K8s API {method} {path} -> {exc.code}") from exc

    def list_target_workloads(self) -> List[dict]:
        return []  # implemented by subclasses; see InClusterKubeClient

    def apply_object(self, obj: dict) -> None:
        group = obj["apiVersion"].split("/")[0]
        kind = obj["kind"].lower() + "s"
        path = f"/apis/{obj['apiVersion']}/namespaces/{obj['metadata']['namespace']}/{kind}/{obj['metadata']['name']}"
        self._request("PUT", path, obj)

    def patch_object(self, api_version, kind, namespace, name, patch) -> None:
        path = f"/apis/{api_version}/namespaces/{namespace}/{kind.lower()}s/{name}"
        self._request("PATCH", path, patch)

    def record_event(self, namespace, involved, reason, message) -> None:
        ev = {
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"namespace": namespace},
            "involvedObject": involved,
            "reason": reason, "message": message,
        }
        self._request("POST", f"/api/v1/namespaces/{namespace}/events", ev)


class RetryingClient:
    """Wraps a KubeClient and retries transient failures with backoff."""

    def __init__(self, inner: KubeClient, retries: int = 3,
                 backoff_seconds: float = 2.0):
        self.inner = inner
        self.retries = retries
        self.backoff_seconds = backoff_seconds

    def _retry(self, fn):
        last = None
        for attempt in range(self.retries + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries:
                    time.sleep(self.backoff_seconds * (attempt + 1))
        raise last

    def list_target_workloads(self):
        return self._retry(self.inner.list_target_workloads)

    def apply_object(self, obj):
        return self._retry(lambda: self.inner.apply_object(obj))

    def patch_object(self, api_version, kind, namespace, name, patch):
        return self._retry(lambda: self.inner.patch_object(
            api_version, kind, namespace, name, patch))

    def record_event(self, namespace, involved, reason, message):
        return self._retry(lambda: self.inner.record_event(
            namespace, involved, reason, message))


def run_loop(client: KubeClient, metrics: MetricsSource,
             cfg: Optional[ControllerConfig] = None, once: bool = False) -> None:
    """Blocking reconcile loop (one pass if ``once``)."""
    cfg = cfg or ControllerConfig()
    while True:
        report = reconcile(client, metrics, cfg)
        print(report.to_dict())
        if once:
            return
        time.sleep(cfg.interval_seconds)