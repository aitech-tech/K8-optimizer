"""Root-Cause Analysis (RCA) over error/action/audit logs + metrics.

The continuous-improvement loop: when something fails or a workload is
repeatedly blocked/evicted, RCA correlates the error log, the action log and
the audit trail to produce a ranked list of probable root causes, each with a
confidence score and suggested remediation.

The analyzer is explainable and deterministic (no black box): it scores
*patterns* — repeated identical errors, a workload repeatedly vetoed by the
safety engine, an OOM/eviction signature, or a persistent low-confidence
signal — and returns the top causes sorted by confidence. This feeds the GUI's
"RCA" panel and the docs' continuous-improvement story.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

from .store import Store


@dataclass
class RootCause:
    title: str
    confidence: float  # 0..1
    evidence: List[str]
    remediation: str
    category: str

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "remediation": self.remediation,
            "category": self.category,
        }


# Signature word set for OOM / eviction / reschedule events.
OOM_SIGNATURES = ("oom", "out of memory", "oomkilled", "evicted",
                  "memory limit", "killed")
RESIZE_SIGNATURES = ("resize", "inplace", "in-place", "restart", "reschedule")
API_SIGNATURES = ("403", "forbidden", "unauthorized", "401", "timeout",
                  "connection", "refused")


def _analyze_errors(errors: List[dict]) -> List[RootCause]:
    causes: List[RootCause] = []
    msgs = [e.get("message", "") for e in errors]
    if not msgs:
        return causes

    # 1. Repeated identical errors -> a systemic defect.
    freqs = Counter(m.lower() for m in msgs)
    top_msg, top_n = (freqs.most_common(1) or [("", 0)])[0]
    if top_n >= 2:
        causes.append(RootCause(
            title=f"Recurring failure ({top_n}×): {top_msg[:80]}",
            confidence=min(1.0, 0.4 + 0.15 * top_n),
            evidence=[f"{top_n} identical error records"],
            remediation="Investigate the repeating code path; add a guard or fix the dependency.",
            category="recurring-error",
        ))

    # 2. OOM / eviction signature -> under-provisioned memory.
    oom = [m for m in msgs if any(s in m.lower() for s in OOM_SIGNATURES)]
    if oom:
        causes.append(RootCause(
            title="Memory pressure / OOM eviction",
            confidence=0.8,
            evidence=oom[:3],
            remediation="Raise the memory request/limit floor; the safety engine "
                        "should have prevented this — check minAllowed.",
            category="memory-pressure",
        ))

    # 3. Resize/restart churn -> strategy mis-selected.
    rs = [m for m in msgs if any(s in m.lower() for s in RESIZE_SIGNATURES)]
    if rs:
        causes.append(RootCause(
            title="Restart churn during optimization",
            confidence=0.6,
            evidence=rs[:3],
            remediation="Ensure InPlacePodVerticalScaling is enabled, or force in-place strategy.",
            category="resize-churn",
        ))

    # 4. Auth/API failures -> RBAC or connectivity.
    api = [m for m in msgs if any(s in m.lower() for s in API_SIGNATURES)]
    if api:
        causes.append(RootCause(
            title="Kubernetes API access failure",
            confidence=0.7,
            evidence=api[:3],
            remediation="Verify the ServiceAccount RBAC grants and API server reachability.",
            category="api-access",
        ))

    return causes


def _analyze_actions(actions: List[dict]) -> List[RootCause]:
    causes: List[RootCause] = []
    blocked = [a for a in actions if a.get("outcome") in ("blocked", "vetoed", "failed")]
    if blocked:
        workloads = Counter(a["workload"] for a in blocked)
        worst_w, worst_n = workloads.most_common(1)[0] if workloads else ("?", 0)
        causes.append(RootCause(
            title=f"Workload '{worst_w}' repeatedly blocked ({worst_n}×)",
            confidence=min(1.0, 0.5 + 0.1 * worst_n),
            evidence=[f"{worst_n} blocked actions for {worst_w}"],
            remediation="Review the safety decision for that workload; either tune "
                        "policy or admit the workload is already optimally sized.",
            category="repeated-block",
        ))
    return causes


def analyze(store: Store, limit: int = 200,
            errors: Optional[List[dict]] = None,
            actions: Optional[List[dict]] = None) -> List[RootCause]:
    """Return ranked root causes from the store's error + action logs."""
    errs = errors if errors is not None else store.list_errors(limit)
    acts = actions if actions is not None else store.list_actions(limit)
    causes = _analyze_errors(errs) + _analyze_actions(acts)
    causes.sort(key=lambda c: c.confidence, reverse=True)
    return causes


def analyze_report(store: Store, limit: int = 200) -> dict:
    causes = analyze(store, limit)
    return {
        "causes": [c.to_dict() for c in causes],
        "count": len(causes),
        "summary": (f"{len(causes)} probable root cause(s) identified "
                    f"from {limit}-record window"),
    }