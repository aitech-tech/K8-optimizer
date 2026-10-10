# K8-Optimizer

Autonomous Kubernetes **cost optimization with zero downtime** — an
open-source implementation of what [ScaleOps](https://scaleops.com) does:
continuously right-size CPU, memory and replica counts from *actual workload
behavior*, instead of hand-tuned request/limit values.

> **Why zero downtime matters.** Classic vertical right-sizing (VPA "Auto"
> mode) *evicts and restarts* pods to apply new requests. K8-Optimizer instead
> prefers **in-place pod resizing** (Kubernetes KEP-1287 / KEP-3685
> `InPlacePodVerticalScaling`) — the running pod's resources are mutated
> directly, with **no eviction, no restart, no dropped traffic**. Where a
> workload can't be resized in place, it falls back to a PDB-aware rolling
> update.

## What it does

| Capability | How |
|---|---|
| **Right-sizing** | CPU → P95 usage + headroom; memory → max usage + buffer (incompressible); replicas → rps / per-replica capacity |
| **Zero downtime** | In-place resize (`pods/resize`) by default; rolling update fallback |
| **Safety gate** | Usage-floor clamping, confidence threshold, max-reduction cap, PDB/QoS awareness |
| **FinOps** | Monthly cost projection, per-workload and cluster-level savings |
| **API** | `POST /recommend` evaluates a batch of workloads and returns actions |
| **Operator** | CRD + RBAC + deployment to run it in-cluster |

## Architecture

```
 metrics (usage samples)
        │
        ▼
 recommender.py   → Recommendation (cpu/mem/replicas + confidence)
        │
        ▼
 safety.py        → SafetyDecision (allow? strategy? floor adjustments)
        │
        ▼
 resize.py        → ordered PatchAction plan (in-place-resize | rolling-update | scale)
        │
        ▼
 cost.py          → CostReport + savings summary
```

The core engines are **pure stdlib Python** — fully unit-testable offline, no
cluster, no third-party deps. The API server uses only `http.server`.

## Quick start

```bash
# Run the engine against example workloads (prints JSON)
python -m k8optimizer.cli --file examples/workloads.json

# Or expose the REST API
python -m k8optimizer.cli --serve --port 8080
# → POST /recommend, GET /health
```

Run tests:

```bash
python -m pytest -q
```

## Deploying in-cluster

```bash
kubectl apply -f deploy/optimizer-crd.yaml
kubectl apply -f deploy/rbac.yaml
kubectl apply -f deploy/deployment.yaml

# Attach the optimizer to a workload
kubectl apply -f deploy/example-optimization.yaml
```

Requires the cluster feature gate for zero-downtime resizes:
`InPlacePodVerticalScaling=true` (K8s ≥ 1.27, beta). Without it, the engine
auto-falls back to rolling updates.

## Workload JSON schema (API / CLI input)

```json
{
  "workloads": [
    {
      "name": "checkout-svc",
      "namespace": "prod",
      "kind": "Deployment",
      "replicas": 8,
      "cpu_request_millis": 2000,
      "memory_request_mib": 4096,
      "qos_class": "Burstable",
      "allow_in_place_resize": true,
      "requests_per_sec": 1200,
      "capacity_per_replica_rps": 300,
      "usage": [
        { "cpu_millis": 250, "memory_mib": 900 }
      ]
    }
  ]
}
```

## Research & prior art

See [`docs/RESEARCH.md`](docs/RESEARCH.md) for the open-source survey this
project draws on — Karpenter, VPA, KEDA, descheduler, OpenCost/Kubecost,
Goldilocks, and Doordash's elastic-cpu-pod-autoscaler — and how each feature
maps into K8-Optimizer.

## License

MIT