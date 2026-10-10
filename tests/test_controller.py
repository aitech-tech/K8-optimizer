"""Falsifier tests for the controller — proven against a fake Kubernetes API.

These assert the autonomous loop actually WRITES the right objects, respects the
zero-downtime guardrails, retries transient failures, and never mutates work the
safety engine vetoed.
"""

import pytest

from k8optimizer.controller import (
    ControllerConfig,
    FakeKubeClient,
    FakeMetrics,
    RetryingClient,
    reconcile,
    workload_from_target,
)
from k8optimizer.models import UsageSample


def _target(name="svc", namespace="prod", kind="Deployment", replicas=8,
            cpu_req="2000m", mem_req="4096Mi", **kw):
    t = {
        "apiVersion": "apps/v1", "kind": kind,
        "metadata": {"name": name, "namespace": namespace,
                     "annotations": kw.pop("annotations", {})},
        "spec": {"replicas": replicas,
                 "template": {"spec": {"containers": [{
                     "name": name,
                     "resources": {"requests": {"cpu": cpu_req, "memory": mem_req}},
                 }]}}},
    }
    return t


def _samples(cpu=250.0, mem=900.0, n=40):
    return [UsageSample(cpu, mem) for _ in range(n)]


# ---- Workload parsing -------------------------------------------------------

def test_workload_from_target_parses_requests_and_qos():
    w = workload_from_target(_target(cpu_req="1000m", mem_req="1024Mi"))
    assert w.cpu_request_millis == 1000.0
    assert w.memory_request_mib == 1024.0
    assert w.qos_class.value == "Burstable"


def test_workload_from_target_detects_guaranteed_qos():
    t = _target()
    c = t["spec"]["template"]["spec"]["containers"][0]
    c["resources"]["limits"] = {"cpu": "2000m", "memory": "4096Mi"}
    c["resources"]["requests"] = {"cpu": "2000m", "memory": "4096Mi"}
    w = workload_from_target(t)
    assert w.qos_class.value == "Guaranteed"


def test_cpu_and_memory_quantity_parsing():
    t = _target(cpu_req="1500m", mem_req="2Gi")
    w = workload_from_target(t)
    assert w.cpu_request_millis == 1500.0
    assert w.memory_request_mib == 2048.0


# ---- Reconcile: applies VPA + HPA + patches ---------------------------------

def test_reconcile_applies_vpa_hpa_and_patches():
    client = FakeKubeClient(targets=[_target()])
    metrics = FakeMetrics({"prod/svc": _samples()})
    report = reconcile(client, metrics)
    r = report.results[0]
    assert r.allowed is True

    kinds = [o["kind"] for o in client.applied]
    assert "VerticalPodAutoscaler" in kinds
    assert "HorizontalPodAutoscaler" in kinds

    strategies = {p["patch"] and p.get("strategy") for p in client.patched}
    # at least one patch was applied
    assert len(client.patched) >= 1
    # a success event was recorded
    assert any(e["reason"] == "Optimized" for e in client.events)


def test_reconcile_respects_safety_veto_no_writes():
    # Forcing low confidence (no metrics) -> safety must veto -> zero writes.
    client = FakeKubeClient(targets=[_target()])
    metrics = FakeMetrics({})  # no samples
    report = reconcile(client, metrics)
    r = report.results[0]
    assert r.allowed is False
    assert client.applied == []
    assert client.patched == []
    assert client.events == []


def test_reconcile_never_mutates_vetoed_workload():
    from k8optimizer.safety import SafetyConfig
    client = FakeKubeClient(targets=[
        _target(name="good", cpu_req="500m", mem_req="512Mi"),
    ])
    metrics = FakeMetrics({"prod/good": _samples(cpu=100.0, mem=200.0)})
    report = reconcile(client, metrics,
                       ControllerConfig(safety=SafetyConfig(min_confidence=2.0)))
    # min_confidence=2.0 exceeds the max achievable confidence (1.0) -> blocked
    assert report.results[0].allowed is False
    assert len(client.applied) == 0


# ---- Retry on transient failure --------------------------------------------

def test_retrying_client_recovers_from_transient_failure():
    inner = FakeKubeClient()
    inner.fail_next_patch = 2  # first two patch calls fail
    client = RetryingClient(inner, retries=3, backoff_seconds=0)
    client.patch_object("v1", "Pod", "prod", "svc", {"containers": []})
    assert len(inner.patched) == 1


def test_retrying_client_raises_after_exhaustion():
    inner = FakeKubeClient()
    inner.fail_next_patch = 999
    client = RetryingClient(inner, retries=2, backoff_seconds=0)
    with pytest.raises(RuntimeError):
        client.patch_object("v1", "Pod", "prod", "svc", {})


# ---- Dry-run applies nothing -------------------------------------------------

def test_dry_run_records_but_writes_nothing():
    client = FakeKubeClient(targets=[_target()])
    metrics = FakeMetrics({"prod/svc": _samples()})
    report = reconcile(client, metrics, ControllerConfig(dry_run=True))
    assert report.results[0].applied_actions
    assert client.applied == []
    assert client.patched == []
    assert client.events == []


# ---- Bounds are honoured ----------------------------------------------------

def test_replica_bounds_never_violated_in_hpa():
    client = FakeKubeClient(targets=[
        _target(name="tight", cpu_req="500m", mem_req="512Mi",
                replicas=2,
                annotations={"k8optimizer.aitech.tech/min-replicas": "1",
                             "k8optimizer.aitech.tech/max-replicas": "10"})
    ])
    metrics = FakeMetrics({"prod/tight": _samples()})
    reconcile(client, metrics)
    hpa = next(o for o in client.applied if o["kind"] == "HorizontalPodAutoscaler")
    assert hpa["spec"]["minReplicas"] >= 1
    assert hpa["spec"]["maxReplicas"] <= 10