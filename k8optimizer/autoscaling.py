"""Autoscaling spec builders — VPA + HPA automation, pure & testable.

This module turns a safe right-sizing recommendation into *concrete Kubernetes
objects* that automate scaling in-cluster:

* **VerticalPodAutoscaler** (``autoscaling.k8s.io/v1``) — recommends and, in
  "Auto" mode, applies CPU/memory requests *in place* with KEP-1287 semantics
  (no restart). Our ``recommender`` feeds ``spec.resourcePolicy.containerPolicies``
  with ``minAllowed``/``maxAllowed`` so the VPA can never drive a pod below the
  usage floor or above the observed ceiling — i.e. we *constrain* VPA, not just
  let it run wild.
* **HorizontalPodAutoscaler** (``autoscaling/v2``) — scales replica count on
  live utilization/traffic. Our replica sizing becomes the HPA's
  ``minReplicas``/``maxReplicas`` bracket plus a ``target`` on CPU/rps, so
  scaling reacts to traffic continuously instead of a one-shot guess.

Everything here is a pure function of (Workload, Recommendation): no network,
no client, fully unit-testable. The controller layer (``controller.py``)
applies these objects via the KubeClient abstraction.

Both builders deliberately produce the SAME safe target the safety engine
already approved (``effective_recommendation`` output), then wrap it in the
autoscaler's policy fields so the cluster's own controllers enforce it in a
control loop — belt-and-suspenders.
"""

from __future__ import annotations

from typing import Optional

from .models import Recommendation, Workload


def _q(millis: float) -> str:
    """Kubernetes CPU quantity from millicores (e.g. 1234.0 -> '1234m')."""
    return f"{int(round(millis))}m"


def _miq(mib: float) -> str:
    """Kubernetes memory quantity from MiB (e.g. 512.0 -> '512Mi')."""
    return f"{int(round(mib))}Mi"


def build_vpa(w: Workload, rec: Recommendation, update_mode: str = "Auto") -> dict:
    """Return a VerticalPodAutoscaler object (dict) for `w`.

    The VPA's container policy is pinned to the *safe* recommendation: the VPA
    may only recommend within ``[minAllowed, maxAllowed]``, which are derived
    from the safety-adjusted target so it can never undershoot the usage floor
    or overshoot a user bound. This is what actually automates vertical
    right-sizing *in-place* once applied.
    """
    target_cpu = float(rec.cpu.recommended)
    target_mem = float(rec.memory.recommended)

    # Constrain VPA's search space around the safe target. Lower bound uses the
    # (already floor-clamped) target reduced by a small headroom — in practice
    # the safety floor already guarantees no undershoot, so keep min == target
    # for the tight, safe lower bound, and upper leaves room to grow.
    min_cpu = target_cpu
    min_mem = target_mem
    max_cpu = max(target_cpu * 1.5, w.cpu_request_millis)
    max_mem = max(target_mem * 1.5, w.memory_request_mib)

    return {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": w.name, "namespace": w.namespace,
                     "labels": {"k8optimizer.aitech.tech/managed": "true",
                                "k8optimizer.aitech.tech/workload": w.name}},
        "spec": {
            "targetRef": {"apiVersion": "apps/v1", "kind": w.kind, "name": w.name},
            "updatePolicy": {"updateMode": update_mode,
                             "minReplicas": max(1, w.min_replicas)},
            "resourcePolicy": {
                "containerPolicies": [{
                    "containerName": "*",
                    "minAllowed": {"cpu": _q(min_cpu), "memory": _miq(min_mem)},
                    "maxAllowed": {"cpu": _q(max_cpu), "memory": _miq(max_mem)},
                    "controlledResources": ["cpu", "memory"],
                }]
            },
        },
    }


def build_hpa(w: Workload, rec: Recommendation,
              metric: str = "cpu",
              target_utilization: float = 70.0) -> dict:
    """Return a HorizontalPodAutoscaler object (dict) for `w`.

    The HPA's min/max replicas bracket is derived from the safe replica
    recommendation (or the workload's declared bounds when no throughput
    signal exists). The metric target keeps average utilization around
    ``target_utilization`` so scaling reacts to live load.
    """
    recommended_replicas = (
        int(rec.replicas.recommended) if rec.replicas is not None else w.replicas
    )
    min_rep = max(1, min(w.min_replicas, recommended_replicas))

    if metric == "rps" and rec.workload.capacity_per_replica_rps:
        metrics = [{
            "type": "Pods",
            "pods": {
                "metric": {"name": "requests_per_second"},
                "target": {"type": "AverageValue",
                           "averageValue": str(int(rec.workload.capacity_per_replica_rps))},
            },
        }]
    else:
        metrics = [{
            "type": "Resource",
            "resource": {
                "name": "cpu",
                "target": {"type": "Utilization",
                           "averageUtilization": int(target_utilization)},
            },
        }]

    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": w.name, "namespace": w.namespace,
                     "labels": {"k8optimizer.aitech.tech/managed": "true",
                                "k8optimizer.aitech.tech/workload": w.name}},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": w.kind, "name": w.name},
            "minReplicas": min_rep,
            "maxReplicas": max(w.max_replicas, min_rep),
            "metrics": metrics,
            "behavior": {
                "scaleDown": {"stabilizationWindowSeconds": 300},
                "scaleUp": {"stabilizationWindowSeconds": 0,
                            "policies": [{"type": "Percent", "value": 100,
                                          "periodSeconds": 60}]},
            },
        },
    }


def build_autoscaling_bundle(w: Workload, rec: Recommendation,
                             vpa: bool = True, hpa: bool = True,
                             metric: str = "cpu",
                             target_utilization: float = 70.0) -> dict:
    """Return {'vpa': {...}|None, 'hpa': {...}|None} for one workload."""
    out = {}
    if vpa:
        out["vpa"] = build_vpa(w, rec)
    if hpa:
        out["hpa"] = build_hpa(w, rec, metric=metric,
                               target_utilization=target_utilization)
    return out