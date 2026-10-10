"""Core data model for K8-Optimizer.

Units: CPU is expressed in millicores (1000m == 1 vCPU), memory in MiB.
Money is expressed in the smallest-currency unit as floats (dollars) for
simplicity; the cost engine keeps a per-Cluster ``currency`` symbol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Sequence


class Resource(Enum):
    CPU = "cpu"
    MEMORY = "memory"


class QoSClass(str, Enum):
    """Kubernetes Quality-of-Service class. Drives safety headroom."""

    GUARANTEED = "Guaranteed"
    BURSTABLE = "Burstable"
    BEST_EFFORT = "BestEffort"


@dataclass(frozen=True)
class UsageSample:
    """One observation of a workload's actual resource consumption."""

    cpu_millis: float  # CPU usage in millicores
    memory_mib: float  # memory usage in MiB
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def pct(values: Sequence[float], percentile: float) -> float:
        """Nearest-rank percentile (what VPA/ScaleOps-style recommenders use)."""
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, int(round(percentile / 100.0 * (len(ordered) - 1)))))
        return float(ordered[idx])

    @staticmethod
    def max_of(values: Sequence[float]) -> float:
        return max(values) if values else 0.0


@dataclass
class Workload:
    """A unit of Kubernetes compute we optimize: a Deployment/StatefulSet/DaemonSet."""

    name: str
    namespace: str
    kind: str = "Deployment"
    replicas: int = 1
    cpu_request_millis: float = 0.0
    cpu_limit_millis: Optional[float] = None  # None == no limit
    memory_request_mib: float = 0.0
    memory_limit_mib: Optional[float] = None
    qos_class: QoSClass = QoSClass.BURSTABLE
    # Historical usage observations over the optimization window.
    usage: List[UsageSample] = field(default_factory=list)
    # Workload-specific throughput signals (requests/sec) for replica sizing.
    requests_per_sec: float = 0.0
    capacity_per_replica_rps: Optional[float] = None
    # Operator preferences / annotations rolled into the resource.
    min_replicas: int = 1
    max_replicas: int = 100
    allow_in_place_resize: bool = True
    # User-provided hourly price inputs are taken from the cost engine instead.

    def cpu_usage(self) -> List[float]:
        return [s.cpu_millis for s in self.usage]

    def memory_usage(self) -> List[float]:
        return [s.memory_mib for s in self.usage]

    def has_signal(self) -> bool:
        return bool(self.usage)


@dataclass
class RightSizeSuggestion:
    """A single resource dimension the recommender proposes changing."""

    resource: Resource
    current: float
    recommended: float
    reason: str

    @property
    def delta_pct(self) -> float:
        if self.current <= 0:
            return 0.0
        return (self.recommended - self.current) / self.current * 100.0


@dataclass
class Recommendation:
    """The full output of the recommender for one Workload."""

    workload: Workload
    cpu: RightSizeSuggestion
    memory: RightSizeSuggestion
    replicas: Optional[RightSizeSuggestion] = None
    confidence: float = 1.0  # 0..1, driven by sample count / volatility
    notes: List[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return bool(self.cpu.delta_pct or self.memory.delta_pct or (self.replicas and self.replicas.delta_pct))


@dataclass
class SafetyDecision:
    """Result of the zero-downtime safety gate."""

    allowed: bool
    reason: str
    strategy: str = "in-place-resize"  # "in-place-resize" | "rolling-update" | "none"
    adjustments: List[str] = field(default_factory=list)


@dataclass
class CostLine:
    label: str
    cpu_cost: float
    memory_cost: float
    total: float

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "cpu_cost": round(self.cpu_cost, 4),
            "memory_cost": round(self.memory_cost, 4),
            "total": round(self.total, 4),
        }


@dataclass
class CostReport:
    workload_name: str
    namespace: str
    baseline: CostLine
    optimized: CostLine
    currency: str = "USD"
    monthly_hours: float = 730.0  # ~ average hours/month

    @property
    def savings(self) -> float:
        return self.baseline.total - self.optimized.total

    @property
    def savings_pct(self) -> float:
        if self.baseline.total <= 0:
            return 0.0
        return self.savings / self.baseline.total * 100.0

    def to_dict(self) -> dict:
        return {
            "workload_name": self.workload_name,
            "namespace": self.namespace,
            "currency": self.currency,
            "monthly_hours": self.monthly_hours,
            "baseline": self.baseline.to_dict(),
            "optimized": self.optimized.to_dict(),
            "savings": round(self.savings, 4),
            "savings_pct": round(self.savings_pct, 4),
        }