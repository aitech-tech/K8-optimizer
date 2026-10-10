"""Falsifier tests — adversarial inputs aimed at breaking the engine's
zero-downtime and correctness guarantees.

These are NOT happy-path tests. Each one tries to make the recommender, the
safety engine, or the cost/resize layers produce a wrong or unsafe result
(negative confidence, a resize below real usage, a JSON payload the CLI/API
cannot serialize, a replica target outside the declared bounds, a crash on
degenerate throughput signals, etc.).
"""

import json

import pytest

from k8optimizer.cost import report_workload
from k8optimizer.models import QoSClass, UsageSample, Workload
from k8optimizer.recommender import RecommenderConfig, recommend
from k8optimizer.resize import build_plan
from k8optimizer.safety import (
    SafetyConfig,
    approve,
    effective_recommendation,
)
from k8optimizer.server import evaluate


def _wk(**kw):
    kw.setdefault("name", "svc")
    kw.setdefault("namespace", "prod")
    kw.setdefault("cpu_request_millis", 1000.0)
    kw.setdefault("memory_request_mib", 1024.0)
    kw.setdefault("usage", [UsageSample(100.0, 200.0) for _ in range(30)])
    return Workload(**kw)


# ---- Recommender falsifiers -------------------------------------------------

def test_negative_capacity_per_replica_does_not_crash():
    """A negative capacity (possible from a mis-scaled metric) must not raise
    and must not emit a bogus replica recommendation."""
    w = _wk(requests_per_sec=100.0, capacity_per_replica_rps=-5.0, replicas=4)
    rec = recommend(w)
    assert rec.replicas is None  # invalid signal -> no resp sizing


def test_zero_capacity_does_not_crash():
    w = _wk(requests_per_sec=100.0, capacity_per_replica_rps=0.0, replicas=4)
    rec = recommend(w)
    assert rec.replicas is None


def test_confidence_is_within_unit_interval():
    """Confidence must always be a valid probability, even for wild data."""
    cases = [
        [],
        [UsageSample(1e9, 1e9)],  # absurdly large
        [UsageSample(0.0, 0.0) for _ in range(30)],
        [UsageSample(float(i), float(i)) for i in range(1, 60)],
    ]
    for usage in cases:
        c = recommend(_wk(usage=usage)).confidence
        assert 0.0 <= c <= 1.0


def test_zero_usage_never_produces_positive_sizing():
    """If usage is all zeros, recommendation must not invent headroom demand."""
    w = _wk(usage=[UsageSample(0.0, 0.0) for _ in range(30)],
            cpu_request_millis=5000.0, memory_request_mib=5000.0)
    rec = recommend(w)
    assert rec.cpu.recommended < rec.cpu.current
    assert rec.memory.recommended < rec.memory.current


def test_peak_spike_is_bounded_by_max_reduction():
    """Even a violent spike cannot push a recommendation past an allowed floor."""
    w = _wk(usage=[UsageSample(5000.0, 5000.0) for _ in range(40)],
            cpu_request_millis=1000.0, memory_request_mib=1000.0)
    rec = recommend(w, RecommenderConfig(max_reduction_pct=0.0))
    # 0% max-reduction -> recommended can never be lower than current.
    assert rec.cpu.recommended >= rec.cpu.current - 1e-6
    assert rec.memory.recommended >= rec.memory.current - 1e-6


# ---- Safety falsifiers ------------------------------------------------------

def test_effective_recommendation_never_goes_below_usage_floor():
    """Guarantee: the value reaching the cluster is never under (100-floor)% of
    the minimum observed usage — the core no-OOM/no-thrash invariant."""
    usage = [UsageSample(900.0, 900.0) for _ in range(30)]  # floor ~ 810
    w = _wk(usage=usage, cpu_request_millis=4000.0, memory_request_mib=4000.0)
    rec = recommend(w)
    eff = effective_recommendation(rec, approve(rec))
    assert eff.cpu.recommended >= 810.0
    assert eff.memory.recommended >= 810.0


def test_floor_adjustment_only_ever_raises():
    """effective_recommendation must never LOWER a value below what the
    recommender proposed — it can only clamp upward to the floor."""
    w = _wk(cpu_request_millis=4000.0, memory_request_mib=4000.0,
            usage=[UsageSample(100.0, 100.0) for _ in range(30)])
    rec = recommend(w)
    dec = approve(rec, SafetyConfig(usage_floor_pct=10.0))
    eff = effective_recommendation(rec, dec)
    assert eff.cpu.recommended >= rec.cpu.recommended - 1e-6
    assert eff.memory.recommended >= rec.memory.recommended - 1e-6


def test_upscaled_workload_is_allowed():
    """Safety must approve *increases* too (under-provisioned workloads)."""
    w = _wk(usage=[UsageSample(1900.0, 1900.0) for _ in range(30)],
            cpu_request_millis=1000.0, memory_request_mib=1024.0)
    dec = approve(recommend(w))
    assert dec.allowed is True


# ---- No-downtime (resize) falsifiers ---------------------------------------

def test_in_place_plan_never_schedules_a_restart():
    """When in-place resize is chosen, the plan MUST NOT contain a rolling
    update (which restarts pods) — this is the zero-downtime contract."""
    w = _wk(allow_in_place_resize=True)
    rec = recommend(w)
    dec = approve(rec)
    plan = build_plan(effective_recommendation(rec, dec), dec)
    strategies = {a.strategy for a in plan}
    assert "in-place-resize" in strategies
    assert "rolling-update" not in strategies


def test_guaranteed_qos_never_rolls():
    """A Guaranteed pod is stateful & latency-sensitive: forcing in-place must
    never fall back to rolling."""
    w = _wk(qos_class=QoSClass.GUARANTEED,
            allow_in_place_resize=False)  # in-place unavailable!
    dec = approve(recommend(w), require_in_place_for_qos=True)
    plan = build_plan(recommend(w), dec)
    assert all(a.strategy != "rolling-update" for a in plan)


def test_replica_target_within_bounds():
    """Replica recommendation stays within [min_replicas, max_replicas]."""
    w = _wk(requests_per_sec=10_000.0, capacity_per_replica_rps=1.0,
            replicas=2, min_replicas=1, max_replicas=50)
    rec = recommend(w)
    assert rec.replicas is not None
    assert 1 <= int(rec.replicas.recommended) <= 50


# ---- Serialization falsifier (the bug the CLI demo caught) -----------------

def test_evaluate_output_is_json_serializable():
    """The CLI and HTTP API both json.dumps(evaluate(...)) — this must not
    raise TypeError (the exact failure the live demo surfaced)."""
    w = _wk(requests_per_sec=500.0, capacity_per_replica_rps=100.0, replicas=8)
    out = evaluate([w])
    dumped = json.dumps(out)  # raises if any dataclass leaks through
    assert isinstance(json.loads(dumped), dict)


def test_cost_report_is_json_serializable():
    w = _wk()
    rep = report_workload(w, recommend(w))
    json.dumps(rep.to_dict())  # must not raise