"""FinOps cost allocation & savings projection.

Prices are supplied per workload (so the same engine works for AWS/GCP/Azure
or on-prem): cost per vCPU-hour and per GiB-month. The engine converts a
Workload's current and optimized resource requests into monthly run cost and
reports savings, in the same spirit as OpenCost/Kubecost allocation with
ScaleOps' real-time savings delta.

Memory prices are usually quoted per "GiB-month" (storage-style) in OpenCost;
we accept dollars per GiB-month and convert MiB -> GiB internally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import CostLine, CostReport, Recommendation, Workload


@dataclass
class CostConfig:
    cpu_usd_per_vcpu_hour: float = 0.0400   # e.g. ~on-demand commodity price
    memory_usd_per_gib_month: float = 2.00   # per GiB, per month
    currency: str = "USD"
    monthly_hours: float = 730.0


def _cpu_hours(request_millis: float, replicas: int, hours: float) -> float:
    return (request_millis / 1000.0) * replicas * hours


def _mem_gib(request_mib: float, replicas: int) -> float:
    return (request_mib / 1024.0) * replicas


def _line(label: str, cpu_req: float, mem_req: float, replicas: int, cfg: CostConfig) -> CostLine:
    cpu_cost = _cpu_hours(cpu_req, replicas, cfg.monthly_hours) * cfg.cpu_usd_per_vcpu_hour
    mem_cost = _mem_gib(mem_req, replicas) * cfg.memory_usd_per_gib_month
    return CostLine(label=label, cpu_cost=cpu_cost, memory_cost=mem_cost, total=cpu_cost + mem_cost)


def report_workload(w: Workload, recommendation: Optional[Recommendation] = None,
                    cfg: Optional[CostConfig] = None) -> CostReport:
    cfg = cfg or CostConfig()
    baseline = _line("baseline", w.cpu_request_millis, w.memory_request_mib, w.replicas, cfg)

    if recommendation is not None:
        eff_replicas = int(recommendation.replicas.recommended) if recommendation.replicas else w.replicas
        optimized = _line("optimized", recommendation.cpu.recommended,
                          recommendation.memory.recommended, eff_replicas, cfg)
    else:
        optimized = baseline

    return CostReport(workload_name=w.name, namespace=w.namespace,
                      baseline=baseline, optimized=optimized,
                      currency=cfg.currency, monthly_hours=cfg.monthly_hours)


def summarize(reports: list[CostReport], currency: str = "USD") -> dict:
    base = sum(r.baseline.total for r in reports)
    opt = sum(r.optimized.total for r in reports)
    savings = base - opt
    return {
        "workloads": len(reports),
        "baseline_monthly": round(base, 2),
        "optimized_monthly": round(opt, 2),
        "savings_monthly": round(savings, 2),
        "savings_pct": round((savings / base * 100.0) if base else 0.0, 2),
        "currency": currency,
    }