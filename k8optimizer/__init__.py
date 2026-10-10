"""K8-Optimizer: autonomous Kubernetes cost optimization with zero-downtime.

A self-contained engine that mirrors what ScaleOps does: continuously
right-sizes CPU, memory and replica counts from real workload behavior, with
zero-downtime guardrails (in-place pod resizing / KEP-1287, PDB-aware
rollouts, usage-floor clamping) and FinOps cost accounting.

The core engines (``recommender``, ``safety``, ``cost``, ``resize``) are pure
Python and depend only on the standard library, so they are fully unit-testable
without a live cluster or any third-party install.
"""

__version__ = "0.1.0"

__all__ = [
    "models",
    "recommender",
    "safety",
    "cost",
    "resize",
    "autoscaling",
    "controller",
    "providers",
    "store",
    "rca",
    "api",
]