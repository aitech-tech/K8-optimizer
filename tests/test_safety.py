from k8optimizer.models import QoSClass, UsageSample, Workload
from k8optimizer.recommender import recommend
from k8optimizer.safety import SafetyConfig, approve, effective_recommendation


def _wk(usage, **kw):
    return Workload(name="api", namespace="prod",
                    cpu_request_millis=kw.pop("cpu_request_millis", 1000.0),
                    memory_request_mib=kw.pop("memory_request_mib", 1024.0),
                    usage=usage, **kw)


def samples(cpu, mem, n=30):
    return [UsageSample(cpu_millis=cpu, memory_mib=mem) for _ in range(n)]


def test_low_confidence_is_blocked():
    w = _wk([])
    rec = recommend(w)
    dec = approve(rec, SafetyConfig(min_confidence=0.5))
    assert dec.allowed is False
    assert dec.strategy == "none"


def test_in_place_resize_chosen_when_supported():
    w = _wk(samples(100.0, 200.0), allow_in_place_resize=True)
    rec = recommend(w)  # usage far below request -> shrink
    dec = approve(rec)
    assert dec.allowed is True
    assert dec.strategy == "in-place-resize"


def test_rolling_update_when_in_place_unavailable():
    w = _wk(samples(100.0, 200.0), allow_in_place_resize=False)
    dec = approve(recommend(w))
    assert dec.allowed is True
    assert dec.strategy == "rolling-update"


def test_pdb_critical_workload_forces_in_place():
    w = _wk(samples(100.0, 200.0), allow_in_place_resize=True, qos_class=QoSClass.GUARANTEED)
    dec = approve(recommend(w), require_in_place_for_qos=True)
    assert dec.allowed is True
    assert dec.strategy == "in-place-resize"


def test_strict_mode_blocks_without_in_place_support():
    w = _wk(samples(100.0, 200.0), allow_in_place_resize=False)
    dec = approve(recommend(w), SafetyConfig(require_in_place=True))
    assert dec.allowed is False


def test_usage_floor_prevents_over_reduction():
    # usage never drops below 900m / 900Mi -> floor at ~810/810
    w = _wk(samples(900.0, 900.0), cpu_request_millis=4000.0, memory_request_mib=4000.0)
    rec = recommend(w)
    eff = effective_recommendation(rec, approve(rec))
    assert eff.cpu.recommended >= 810.0
    assert eff.memory.recommended >= 810.0


def test_insignificant_change_is_blocked():
    w = _wk(samples(950.0, 500.0), cpu_request_millis=1000.0, memory_request_mib=1024.0)
    rec = recommend(w)
    dec = approve(rec, SafetyConfig(min_delta_pct=80.0))
    assert dec.allowed is False