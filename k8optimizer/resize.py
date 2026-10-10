"""Resize application layer — how a safe recommendation becomes reality.

Two modes, mirroring ScaleOps' zero-downtime guarantee:

* **in-place-resize**: POST a pod's resource mutations via the K8s
  ``/resize`` subresource (KEP-1287). No eviction, no restart, no downtime.
  Emitted as a concrete patch action the operator (or a user) applies.
* **rolling-update**: patch the controller's pod template; new pods roll in
  while old pods serve traffic (honours ``maxUnavailable``/PDBs).

This module is deliberately side-effect free: it *produces* the exact
operations to apply, and returns them as an ordered plan (PatchAction list).
The in-cluster operator turns those into API calls, so this logic stays fully
testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .models import Recommendation, SafetyDecision, Workload


@dataclass
class PatchAction:
    api_version: str
    kind: str
    namespace: str
    name: str
    strategy: str
    # Resource patch body (JSON-ish dict) to apply.
    patch: dict
    description: str


def build_plan(rec: Recommendation, decision: SafetyDecision) -> List[PatchAction]:
    """Return the ordered list of zero-downtime actions for one workload."""
    if not decision.allowed:
        return []
    w = rec.workload
    actions: List[PatchAction] = []

    if decision.strategy == "in-place-resize":
        # In-place: mutate the running pods directly (no template change means
        # no re-roll, no restart). This is the downtime-free fast path.
        actions.append(PatchAction(
            api_version="v1", kind="Pod", namespace=w.namespace, name=f"{w.name} (label selector)",
            strategy="in-place-resize",
            patch={
                "containers": [{
                    "name": w.name,
                    "resources": {
                        "requests": {
                            "cpu": f"{rec.cpu.recommended:.0f}m",
                            "memory": f"{rec.memory.recommended:.0f}Mi",
                        },
                        "limits": {
                            "cpu": f"{rec.cpu.recommended:.0f}m",
                            "memory": f"{rec.memory.recommended:.0f}Mi",
                        },
                    },
                }],
            },
            description="Resize live pods in place (KEP-1287 /resize) — no restart",
        ))
    else:
        # Rolling update: patch the controller template; old pods keep serving.
        actions.append(PatchAction(
            api_version="apps/v1", kind=w.kind, namespace=w.namespace, name=w.name,
            strategy="rolling-update",
            patch={
                "spec": {"template": {"spec": {"containers": [{
                    "name": w.name,
                    "resources": {
                        "requests": {
                            "cpu": f"{rec.cpu.recommended:.0f}m",
                            "memory": f"{rec.memory.recommended:.0f}Mi",
                        },
                    },
                }]}}},
            },
            description="Rolling update of pod template (maxUnavailable respected)",
        ))

    if rec.replicas is not None and int(rec.replicas.recommended) != w.replicas:
        actions.append(PatchAction(
            api_version="apps/v1", kind=w.kind, namespace=w.namespace, name=w.name,
            strategy="scale-replicas",
            patch={"spec": {"replicas": int(rec.replicas.recommended)}},
            description=f"Scale replicas {w.replicas} -> {int(rec.replicas.recommended)}",
        ))

    return actions


def plan_text(actions: List[PatchAction]) -> str:
    if not actions:
        return "(no action — blocked by safety engine)"
    lines = []
    for a in actions:
        lines.append(f"[{a.strategy}] {a.kind} {a.namespace}/{a.name}: {a.description}")
    return "\n".join(lines)