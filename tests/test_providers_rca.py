"""Tests for cloud providers + RCA."""

import pytest

from k8optimizer.providers import PROVIDERS, get_provider, list_providers
from k8optimizer.rca import analyze, analyze_report
from k8optimizer.store import Store


def test_all_five_providers_present():
    assert set(PROVIDERS) == {"aws", "gcp", "azure", "oci", "onprem"}


def test_provider_cost_config_uses_cloud_pricing():
    aws = get_provider("aws")
    cfg = aws.cost_config()
    assert cfg.cpu_usd_per_vcpu_hour == aws.default_cpu_usd_per_vcpu_hour
    onprem = get_provider("onprem")
    assert onprem.kind == "onprem"
    assert onprem.cost_config().cpu_usd_per_vcpu_hour < aws.cost_config().cpu_usd_per_vcpu_hour


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        get_provider("ibm")


def test_list_providers_is_serializable():
    import json
    json.dumps(list_providers())  # must not raise


def test_each_provider_lists_regions():
    for pid in PROVIDERS:
        assert PROVIDERS[pid].regions


# ---- RCA -------------------------------------------------------------------

def test_rca_detects_recurring_error():
    s = Store(":memory:")
    for _ in range(3):
        s.log_error("controller", "connection refused")
    causes = analyze(s)
    titles = [c.title for c in causes]
    assert any("Recurring" in t for t in titles)
    s.close()


def test_rca_detects_oom_signature():
    s = Store(":memory:")
    s.log_error("controller", "pod OOMKilled: memory limit exceeded")
    causes = analyze(s)
    assert any("Memory pressure" in c.title for c in causes)
    s.close()


def test_rca_detects_repeated_blocked_workload():
    s = Store(":memory:")
    for _ in range(4):
        s.log_action("svc", "prod", "resize", "vetoed", outcome="blocked")
    causes = analyze(s)
    assert any("repeatedly blocked" in c.title for c in causes)
    s.close()


def test_rca_empty_logs_returns_no_causes():
    s = Store(":memory:")
    assert analyze(s) == []
    rep = analyze_report(s)
    assert rep["count"] == 0
    s.close()