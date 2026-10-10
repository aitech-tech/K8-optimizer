import pytest

from k8optimizer.models import QoSClass, UsageSample, Workload
from k8optimizer.recommender import RecommenderConfig, recommend


def _wk(samples, cpu_req=1000.0, mem_req=1024.0, **kw):
    return Workload(name="api", namespace="prod", cpu_request_millis=cpu_req,
                    memory_request_mib=mem_req, usage=samples, **kw)


def steady(start=100.0, step=1.0, n=30):
    return [UsageSample(cpu_millis=start + i * step, memory_mib=200.0 + i * 5) for i in range(n)]


def test_insufficient_samples_returns_noop():
    w = _wk([UsageSample(100.0, 200.0)])
    rec = recommend(w, RecommenderConfig(min_samples_cpu=24, min_samples_memory=12))
    assert rec.cpu.current == rec.cpu.recommended
    assert rec.memory.current == rec.memory.recommended
    assert rec.replicas is None


def test_overprovisioned_cpu_is_reduced():
    w = _wk(steady(start=100.0, step=1.0, n=30), cpu_req=2000.0, mem_req=2048.0)
    rec = recommend(w)
    # usage ~ 114m P95, +20% headroom -> well under 2000m
    assert rec.cpu.recommended < rec.cpu.current
    assert rec.cpu.recommended > 0


def test_memory_uses_max_plus_buffer():
    w = _wk(steady(n=30), cpu_req=1000.0, mem_req=4096.0)
    rec = recommend(w)
    # memory usage peaks ~345Mi, buffer 15% -> ~397Mi, far below 4096Mi
    assert rec.memory.recommended < rec.memory.current
    assert rec.memory.recommended >= 345


def test_max_reduction_cap_is_respected():
    w = _wk([UsageSample(1.0, 9.0) for _ in range(40)], cpu_req=10000.0, mem_req=10000.0)
    rec = recommend(w, RecommenderConfig(max_reduction_pct=90.0))
    # cannot drop below 1000 (90% off 10000)
    assert rec.cpu.recommended >= 1000.0
    assert rec.memory.recommended >= 1000.0


def test_replica_sizing_from_rps():
    w = _wk(steady(n=30), requests_per_sec=800.0, capacity_per_replica_rps=100.0,
            replicas=20, min_replicas=1, max_replicas=100)
    rec = recommend(w)
    assert rec.replicas is not None
    # 800 rps / 100 per replica / 0.7 util = ~11.4 -> 12
    assert int(rec.replicas.recommended) == 12


def test_confidence_rises_with_samples_and_stability():
    volatile = [UsageSample(cpu_millis=500.0 + (i % 2) * 900.0, memory_mib=300) for i in range(30)]
    stable = [UsageSample(cpu_millis=500.0, memory_mib=300) for _ in range(30)]
    assert recommend(_wk(stable)).confidence >= recommend(_wk(volatile)).confidence


def test_zero_usage_signal_gives_zero_confidence():
    w = _wk([])
    assert recommend(w).confidence == 0.0