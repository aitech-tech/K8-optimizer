from k8optimizer.models import UsageSample, Workload
from k8optimizer.recommender import recommend
from k8optimizer.resize import build_plan, plan_text
from k8optimizer.safety import approve, effective_recommendation


def _wk(**kw):
    kw.setdefault("cpu_request_millis", 2000.0)
    kw.setdefault("memory_request_mib", 2048.0)
    kw.setdefault("usage", [UsageSample(100.0, 200.0) for _ in range(30)])
    return Workload(name="api", namespace="prod", **kw)


def test_in_place_plan_has_no_pod_spec_change():
    w = _wk(allow_in_place_resize=True)
    dec = approve(recommend(w))
    plan = build_plan(effective_recommendation(recommend(w), dec), dec)
    assert plan, "expected actions"
    assert any(a.strategy == "in-place-resize" for a in plan)
    assert not any(a.strategy == "rolling-update" for a in plan)


def test_blocked_decision_yields_empty_plan():
    w = _wk(usage=[], allow_in_place_resize=True)
    rec = recommend(w)  # no signal -> low confidence
    dec = approve(rec)
    assert not build_plan(rec, dec)


def test_replica_change_appears_as_scale_action():
    w = _wk(requests_per_sec=500.0, capacity_per_replica_rps=100.0, replicas=20)
    dec = approve(recommend(w))
    plan = build_plan(effective_recommendation(recommend(w), dec), dec)
    assert any(a.strategy == "scale-replicas" for a in plan)


def test_plan_text_describes_actions():
    w = _wk(allow_in_place_resize=True)
    dec = approve(recommend(w))
    plan = build_plan(effective_recommendation(recommend(w), dec), dec)
    assert "in-place-resize" in plan_text(plan)