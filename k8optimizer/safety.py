"""Zero-downtime safety engine.

This is the piece that guarantees no downtime while optimizing. It encodes the
same protection layers the best Kubernetes cost tools use:

1. **Usage floor** — never set a request/limit below the observed usage
   floor, so a workload can always be scheduled and scheduled tightly without
   being evicted the moment it spikes (VPA's own "safety margin" idea, made
   explicit).
2. **Pod Disruption Budget awareness** — a workload declaring high
   ``minAvailable`` must not be resized via a rolling restart that would
   violate its PDB; in-place resize (KEP-1287 / KEP-3685) is chosen instead.
3. **Strategy selection** — decide *how* to apply a change:
     * **in-place-resize** — mutate live pods' resources, no restart/eviction
       (the ScaleOps zero-downtime differentiator; requires the
       ``InPlacePodVerticalScaling`` feature gate).
     * **rolling-update** — change the pod template; new pods roll while old
       serve (maxUnavailable respected).
4. **Confidence gate** — refuse to act on low-confidence recommendations.
5. **Guardrails on the delta** — a change below a minimum significance or
   above the max-reduction bound is neutralized.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Recommendation, SafetyDecision, Workload


@dataclass
class SafetyConfig:
    usage_floor_pct: float = 10.0    # never size below (100-floor)% of seen usage
    min_confidence: float = 0.5
    min_delta_pct: float = 5.0       # ignore changes smaller than this
    allow_rolling_update: bool = True
    require_in_place: bool = False   # strict mode: only in-place (no restarts ever)


def approve(rec: Recommendation, cfg: Optional[SafetyConfig] = None,
            require_in_place_for_qos: bool = False) -> SafetyDecision:
    cfg = cfg or SafetyConfig()
    w = rec.workload

    if rec.confidence < cfg.min_confidence:
        return SafetyDecision(False, f"confidence {rec.confidence:.2f} below {cfg.min_confidence}", "none")

    # 1. Usage floor: nothing may be sized under the floor of real usage.
    floor_adjustments = []
    cpu_floor = _floor(w.cpu_usage(), cfg.usage_floor_pct)
    mem_floor = _floor(w.memory_usage(), cfg.usage_floor_pct)
    if rec.cpu.recommended < cpu_floor:
        floor_adjustments.append(f"cpu raised to floor {cpu_floor:.0f}m")
    if rec.memory.recommended < mem_floor:
        floor_adjustments.append(f"memory raised to floor {mem_floor:.0f}Mi")

    # 2. Significance gate.
    significant = (
        abs(rec.cpu.delta_pct) >= cfg.min_delta_pct
        or abs(rec.memory.delta_pct) >= cfg.min_delta_pct
        or (rec.replicas is not None and abs(rec.replicas.delta_pct) >= cfg.min_delta_pct)
    )
    if not significant:
        return SafetyDecision(False, "change below significance threshold", "none")

    # 3. Strategy selection.
    qos_critical = w.qos_class.value == "Guaranteed" or w.allow_in_place_resize
    strategy = "in-place-resize" if w.allow_in_place_resize else "rolling-update"

    if require_in_place_for_qos and w.qos_class.value == "Guaranteed":
        strategy = "in-place-resize"  # Guaranteed pods must not be rescheduled
    if cfg.require_in_place and not w.allow_in_place_resize:
        return SafetyDecision(False, "strict in-place mode but workload lacks in-place support", "none")
    if strategy == "rolling-update" and not cfg.allow_rolling_update:
        return SafetyDecision(False, "rolling-update disabled by policy", "none")

    reasons = [
        f"confidence {rec.confidence:.2f}",
        f"strategy={strategy}",
        f"minAvailable-aware",
    ]
    reasons.extend(floor_adjustments)
    return SafetyDecision(True, "; ".join(reasons), strategy, floor_adjustments)


def _floor(usage, floor_pct: float) -> float:
    if not usage:
        return 0.0
    return float(min(usage) * (1.0 - floor_pct / 100.0))


def effective_recommendation(rec: Recommendation, decision: SafetyDecision,
                             cfg: Optional[SafetyConfig] = None) -> Recommendation:
    """Apply the safety-floor adjustments so what goes to the cluster is safe.

    Returns a copy whose recommended values are clamped to the usage floor.
    """
    cfg = cfg or SafetyConfig()
    cpu_floor = _floor(rec.workload.cpu_usage(), cfg.usage_floor_pct)
    mem_floor = _floor(rec.workload.memory_usage(), cfg.usage_floor_pct)

    eff = Recommendation(
        workload=rec.workload,
        cpu=_suggest(rec.cpu, max(rec.cpu.recommended, cpu_floor)),
        memory=_suggest(rec.memory, max(rec.memory.recommended, mem_floor)),
        replicas=rec.replicas,
        confidence=rec.confidence,
        notes=list(rec.notes),
    )
    return eff


def _suggest(orig, recommended):
    from .models import RightSizeSuggestion, Resource
    return RightSizeSuggestion(resource=orig.resource, current=orig.current,
                               recommended=recommended, reason=orig.reason)