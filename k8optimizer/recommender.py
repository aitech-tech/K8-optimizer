"""Right-sizing recommender.

Mirrors the recommendation logic ScaleOps and the Kubernetes Vertical Pod
Autoscaler use:

* CPU request/limit  <- P95 (or P99) of observed *usage* + headroom.
* Memory request    <- max observed usage + buffer (memory is incompressible;
                       unlike VPA we never base memory on a mid percentile).
* Replica count     <- observed RPS / measured per-replica capacity, floored
                       by HPA-style throttling heuristics.

Headroom and percentile are tunable so ops can trade cost vs. safety. The
recommender never proposes a change below the safety floor — that is enforced
by ``safety.floor()`` before anything reaches the cluster.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from .models import (
    QoSClass,
    Recommendation,
    Resource,
    RightSizeSuggestion,
    UsageSample,
    Workload,
)


@dataclass
class RecommenderConfig:
    """Tunables for the recommendation policy."""

    cpu_percentile: float = 95.0          # percentile of CPU usage to size to
    cpu_headroom_pct: float = 20.0        # extra on top of the percentile
    memory_headroom_pct: float = 15.0     # buffer above observed memory max
    memory_percentile: float = 100.0      # memory uses max by default
    max_reduction_pct: float = 90.0       # never shrink a request by more than this
    min_confidence: float = 0.5           # below this, recommend "no change"
    min_samples_cpu: int = 24             # enough of a day at hourly cadence
    min_samples_memory: int = 12
    replica_target_cpu_util: float = 70.0  # keep CPU ~70% across replicas


def _suggest(resource: Resource, current: float, recommended: float, reason: str) -> RightSizeSuggestion:
    return RightSizeSuggestion(resource=resource, current=current, recommended=recommended, reason=reason)


def recommend(w: Workload, cfg: Optional[RecommenderConfig] = None) -> Recommendation:
    cfg = cfg or RecommenderConfig()
    notes: List[str] = []
    cpu_s, mem_s = _recommend_cpu(w, cfg), _recommend_memory(w, cfg)
    rep_s = _recommend_replicas(w, cfg, notes)

    # Confidence: more samples, lower volatility -> more confident.
    confidence = _confidence(w, cfg)
    if cpu_s:
        notes.append(cpu_s.reason)
    if mem_s:
        notes.append(mem_s.reason)

    return Recommendation(workload=w, cpu=cpu_s, memory=mem_s, replicas=rep_s,
                          confidence=confidence, notes=notes)


def _recommend_cpu(w: Workload, cfg: RecommenderConfig) -> RightSizeSuggestion:
    current = w.cpu_request_millis
    usage = w.cpu_usage()
    if len(usage) < cfg.min_samples_cpu:
        return _suggest(Resource.CPU, current, current, "insufficient CPU samples")
    p = UsageSample.pct(usage, cfg.cpu_percentile)
    target = p * (1.0 + cfg.cpu_headroom_pct / 100.0)
    target = max(target, current * (1.0 - cfg.max_reduction_pct / 100.0))
    target = round(target / 10.0) * 10.0  # 10m granularity
    return _suggest(Resource.CPU, current, target, f"CPU P{cfg.cpu_percentile:.0f}={p:.0f}m + {cfg.cpu_headroom_pct:.0f}% headroom")


def _recommend_memory(w: Workload, cfg: RecommenderConfig) -> RightSizeSuggestion:
    current = w.memory_request_mib
    usage = w.memory_usage()
    if len(usage) < cfg.min_samples_memory:
        return _suggest(Resource.MEMORY, current, current, "insufficient memory samples")
    base = UsageSample.pct(usage, cfg.memory_percentile)
    target = base * (1.0 + cfg.memory_headroom_pct / 100.0)
    target = max(target, current * (1.0 - cfg.max_reduction_pct / 100.0))
    target = round(target)
    return _suggest(Resource.MEMORY, current, target, f"memory max={base:.0f}Mi + {cfg.memory_headroom_pct:.0f}% buffer")


def _recommend_replicas(w: Workload, cfg: RecommenderConfig, notes: List[str]) -> Optional[RightSizeSuggestion]:
    if w.capacity_per_replica_rps and w.requests_per_sec > 0:
        per = max(0.0, w.capacity_per_replica_rps)
        if per <= 0:
            return None  # invalid (non-positive) capacity signal -> no sizing
        needed = int(math.ceil(
            (w.requests_per_sec / per) / (cfg.replica_target_cpu_util / 100.0)
        ))
        needed = max(w.min_replicas, min(w.max_replicas, needed))
        if needed != w.replicas:
            notes.append(f"replicas {w.replicas} -> {needed} to hold ~{cfg.replica_target_cpu_util:.0f}% util at {w.requests_per_sec:.1f} rps")
            return _suggest(Resource.CPU, float(w.replicas), float(needed),
                            f"rps/capacity suggests {needed} replicas")
    return None


def _confidence(w: Workload, cfg: RecommenderConfig) -> float:
    cpu, mem = w.cpu_usage(), w.memory_usage()
    if not cpu and not mem:
        return 0.0
    n = max(len(cpu), len(mem))
    sample_f = min(1.0, n / float(cfg.min_samples_cpu))
    # Volatility: coefficient of variation of CPU usage.
    if cpu and len(cpu) > 1:
        mean = sum(cpu) / len(cpu)
        if mean > 0:
            var = sum((x - mean) ** 2 for x in cpu) / len(cpu)
            cv = math.sqrt(var) / mean
            vol_f = max(0.0, 1.0 - cv)  # high variance -> low confidence
        else:
            vol_f = 1.0
    else:
        vol_f = 1.0
    return round(min(1.0, 0.5 * sample_f + 0.5 * vol_f), 3)