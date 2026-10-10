"""Tests for the VPA + HPA automation builders."""

import pytest

from k8optimizer.autoscaling import (
    build_autoscaling_bundle,
    build_hpa,
    build_vpa,
)
from k8optimizer.models import QoSClass, UsageSample, Workload
from k8optimizer.recommender import recommend
from k8optimizer.safety import approve, effective_recommendation


def _wk(**kw):
    kw.setdefault("name", "svc")
    kw.setdefault("namespace", "prod")
    kw.setdefault("cpu_request_millis", 1000.0)
    kw.setdefault("memory_request_mib", 1024.0)
    kw.setdefault("allow_in_place_resize", True)
    kw.setdefault("usage", [UsageSample(200.0, 300.0) for _ in range(40)])
    return Workload(**kw)


def _eff(w):
    rec = recommend(w)
    dec = approve(rec)
    return effective_recommendation(rec, dec)


def test_vpa_is_valid_object_and_constrains_to_safe_target():
    w = _wk()
    eff = _eff(w)
    vpa = build_vpa(w, eff)
    assert vpa["apiVersion"] == "autoscaling.k8s.io/v1"
    assert vpa["kind"] == "VerticalPodAutoscaler"
    assert vpa["metadata"]["namespace"] == "prod"
    assert vpa["spec"]["targetRef"]["name"] == "svc"
    pol = vpa["spec"]["resourcePolicy"]["containerPolicies"][0]
    # minAllowed must never go below the safe target (no undershoot).
    assert pol["minAllowed"]["cpu"] == f"{int(eff.cpu.recommended)}m"
    assert pol["minAllowed"]["memory"] == f"{int(eff.memory.recommended)}Mi"
    assert vpa["spec"]["updatePolicy"]["updateMode"] == "Auto"


def test_vpa_max_allowed_grows_room_not_shrinks():
    w = _wk()
    eff = _eff(w)
    vpa = build_vpa(w, eff)
    pol = vpa["spec"]["resourcePolicy"]["containerPolicies"][0]
    cpu_max = pol["maxAllowed"]["cpu"]
    # max must be >= min (the safe target).
    assert int(cpu_max.rstrip("m")) >= int(pol["minAllowed"]["cpu"].rstrip("m"))


def test_hpa_bracket_covers_recommended_replicas():
    w = _wk(requests_per_sec=500.0, capacity_per_replica_rps=100.0,
            replicas=2, min_replicas=1, max_replicas=50)
    eff = _eff(w)
    hpa = build_hpa(w, eff)
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["kind"] == "HorizontalPodAutoscaler"
    rec_replicas = int(eff.replicas.recommended)
    assert hpa["spec"]["minReplicas"] <= rec_replicas <= hpa["spec"]["maxReplicas"]
    assert hpa["spec"]["metrics"][0]["resource"]["name"] == "cpu"


def test_hpa_rps_metric_when_capacity_known():
    w = _wk(requests_per_sec=500.0, capacity_per_replica_rps=100.0, replicas=2)
    eff = _eff(w)
    hpa = build_hpa(w, eff, metric="rps")
    assert hpa["spec"]["metrics"][0]["type"] == "Pods"


def test_bundle_omits_hpa_when_requested():
    w = _wk()
    eff = _eff(w)
    bundle = build_autoscaling_bundle(w, eff, hpa=False)
    assert "vpa" in bundle and "hpa" not in bundle


def test_bundle_present_both_by_default():
    w = _wk()
    eff = _eff(w)
    bundle = build_autoscaling_bundle(w, eff)
    assert bundle["vpa"]["kind"] == "VerticalPodAutoscaler"
    assert bundle["hpa"]["kind"] == "HorizontalPodAutoscaler"


def test_qos_guaranteed_workload_still_builds():
    w = _wk(qos_class=QoSClass.GUARANTEED)
    eff = _eff(w)
    vpa = build_vpa(w, eff)
    assert vpa["spec"]["targetRef"]["kind"] == "Deployment"