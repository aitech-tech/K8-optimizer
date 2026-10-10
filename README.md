# K8-Optimizer

Autonomous Kubernetes **cost optimization with zero downtime**, delivered as an
enterprise-grade product: an always-on controller, a permissioned REST API, and
a single-page console — across AWS, GCP, Azure, Oracle Cloud (OCI) and on-prem.

## The two-pillar design

**1. Zero-downtime optimization.** Classic vertical right-sizing (VPA "Auto"
mode) *evicts and restarts* pods to apply new requests. K8-Optimizer instead
prefers **in-place pod resizing** (Kubernetes KEP-1287 / KEP-3685
`InPlacePodVerticalScaling`) — the running pod's resources are mutated directly,
with **no eviction, no restart, no dropped traffic**. Where a workload can't be
resized in place, it falls back to a PDB-aware rolling update.

**2. Autonomous VPA + HPA.** The controller is a closed loop — watch → read real
metrics → recommend → safety-gate → **apply** — that writes real
`VerticalPodAutoscaler` and `HorizontalPodAutoscaler` objects and zero-downtime
patches into the cluster, not just recommendations.

## Capabilities

| Area | What it does |
|---|---|
| **Right-sizing** | CPU → P95 usage + headroom; memory → max usage + buffer (incompressible); replicas → rps / per-replica capacity |
| **Zero downtime** | In-place resize (`pods/resize`) by default; PDB-aware rolling-update fallback |
| **Safety gate** | Usage-floor clamping, confidence threshold, max-reduction cap, significance gating, PDB/QoS awareness |
| **Autoscaling** | Emits VPA + HPA objects constrained to the safety-approved target (VPA can't undershoot the usage floor) |
| **FinOps** | Monthly cost projection, per-workload and cluster-level savings, per-cloud pricing |
| **Controller** | In-cluster reconcile loop that *applies* changes; retries transient API failures; never mutates vetoed work |
| **RBAC** | Roles (admin / optimizer / viewer / auditor) + dot-scoped permissions, PBKDF2 password hashing, HMAC-signed tokens |
| **Audit / action / error logs** | Append-only record of *who did what*, every applied action, and every failure |
| **RCA** | Root-cause analysis over logs — recurring errors, OOM/eviction, resize churn, repeated safety-blocks, API failures — with confidence + remediation |
| **Multi-cloud** | Pricing + region adapters for AWS, GCP, Azure, OCI, and on-prem |
| **GUI** | Single-page console: login, metrics dashboard, logs, RCA, cloud providers, users & permissions |
| **API** | Permissioned JSON endpoints for metrics, logs, RCA, providers, users |

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
        ├──────────► autoscaling.py  → VPA + HPA object builders (bounded by safety)
        │
        ▼
 resize.py        → ordered PatchAction plan (in-place-resize | rolling-update | scale)
        │
        ▼
 controller.py    → reconcile loop: APPLY patches + VPA/HPA to the cluster (KubeClient + MetricsSource)
        │
        ├──────────► cost.py         → CostReport + savings summary
        ├──────────► store.py        → users/roles/permissions + audit/action/error logs (SQLite)
        ├──────────► rca.py          → root-cause analysis over logs
        ├──────────► providers.py    → AWS/GCP/Azure/OCI/on-prem pricing + regions
        └──────────► api.py + server.py + dashboard.html  → REST API + GUI
```

The core engines are **pure stdlib Python** — fully unit-testable offline, no
cluster, no third-party deps. The API server and GUI use only `http.server` and
a dependency-free single HTML file.

## Quick start

```bash
# Run the engine against example workloads (prints JSON)
python -m k8optimizer.cli --file examples/workloads.json

# Or run the full product: REST API + console (persists to k8optimizer.db)
python -m k8optimizer.cli --serve --port 8080
# → open http://127.0.0.1:8080/  (login: admin / admin)
```

The console covers Metrics, Logs (action / audit / error), RCA, Cloud Providers,
and Users & Permissions — every panel gated by the signed-in user's permissions.

Run tests:

```bash
python -m pytest -q
```

## REST API

| Method | Path | Permission | Description |
|---|---|---|---|
| `POST` | `/api/login` | — | `{username, password}` → `{token}` |
| `GET` | `/api/me` | `workload.read` | Profile + effective permissions |
| `GET` | `/api/metrics` | `metrics.read` | Dashboard aggregates (success rate, outcomes, levels, actors) |
| `GET` | `/api/logs/audit\|actions\|errors` | `logs.read` | Log rows |
| `POST` | `/api/logs/errors` | `logs.write` | Record an error |
| `GET` | `/api/rca` | `rca.read` | Ranked root causes |
| `GET` | `/api/providers` | `metrics.read` | Multi-cloud registry |
| `GET` | `/api/users` | `users.manage` | List users |
| `POST` | `/api/users` | `users.manage` | Create user |
| `PUT` | `/api/users/<u>/role` | `users.manage` | Change role |
| `PUT` | `/api/users/<u>/permissions` | `users.manage` | Per-user overrides |
| `POST` | `/recommend` | — | Evaluate a batch (headless/legacy) |

Auth is an HMAC-signed token from `store.py` (`Authorization: Bearer <token>`).
Every mutation writes an audit entry; nothing changes without a trace.

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

## Competitive positioning

See [`docs/POSITIONING.md`](docs/POSITIONING.md) for an honest, feature-by-feature
comparison against VPA, KEDA, Karpenter, OpenCost/Kubecost and ScaleOps — what
this project *implements and verifies* versus what each incumbent does.

## Research & prior art

See [`docs/RESEARCH.md`](docs/RESEARCH.md) for the open-source survey this
project draws on and how each feature maps into K8-Optimizer.

## License

MIT