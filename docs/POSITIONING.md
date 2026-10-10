# Competitive positioning

This document compares K8-Optimizer against the tools teams already run, feature
by feature. It is deliberately **honest**: each row states what K8-Optimizer
*implements and verifies in this repository* (unit-tested against a fake
Kubernetes API) versus what the incumbent ships. It does **not** claim
"best-in-class" superiority — sustained production comparison on live clusters
is the only thing that would justify that, and that is outside what this repo
can demonstrate.

## The one-line summary

Most Kubernetes cost tools do **one slice** of the problem and leave the rest to
you to glue together. K8-Optimizer unifies, behind one permissioned console:

1. **Right-sizing decisions** (what VPA recommends),
2. **Zero-downtime application** (what VPA `Auto` mode gets wrong),
3. **Autoscaling** (VPA + HPA objects, not just advice),
4. **FinOps cost allocation** (what OpenCost/Kubecost show),
5. **Governance + RCA** (RBAC, audit/action/error logs, root-cause analysis).

## Feature matrix

Legend: ✅ = implemented & tested here · ⚠️ = partial · ❌ = not present.

| Capability | K8-Optimizer | VPA | KEDA | Karpenter | OpenCost/Kubecost | ScaleOps |
|---|---|---|---|---|---|---|
| Vertical right-sizing (CPU/mem) | ✅ | ✅ (recommend) | ❌ | ❌ | ❌ | ✅ |
| Horizontal scaling | ✅ (HPA) | ❌ | ✅ | ❌ | ❌ | ✅ |
| **Zero-downtime apply (in-place resize)** | ✅ | ❌ (evicts/restarts) | n/a | n/a | n/a | ✅ |
| PDB-aware strategy choice | ✅ | ❌ | n/a | n/a | n/a | ✅ |
| Usage-floor clamp (never undershoot observed min) | ✅ | ⚠️ | n/a | ❌ | ❌ | ✅ |
| Confidence gate (skip on shaky data) | ✅ | ❌ | ⚠️ | ❌ | ❌ | ⚠️ |
| FinOps cost allocation + savings | ✅ | ❌ | ❌ | ⚠️ (node cost) | ✅ | ✅ |
| Multi-cloud pricing (AWS/GCP/Azure/OCI/on-prem) | ✅ | n/a | n/a | ⚠️ (AWS-first) | ✅ | ✅ |
| RBAC (roles + dot-scoped permissions) | ✅ | ❌ | ❌ | ❌ | ⚠️ (read models) | ✅ |
| Audit / action / error log trail | ✅ | ❌ | ❌ | ❌ | ⚠️ | ✅ |
| Root-cause analysis (explainable) | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ (proprietary) |
| Web console (login, metrics, logs, users) | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ |
| Node autoscaling / bin-packing | ❌ | ❌ | ❌ | ✅ | ❌ | ⚠️ |

### Reading the gaps honestly

- **Karpenter** wins on *node-level* autoscaling and bin-packing — K8-Optimizer
  works at the *workload* level and does not bin-pack nodes. The two are
  complementary, not substitutes.
- **KEDA** is a richer *event-driven* autoscaler (external scalers). K8-Optimizer
  ships a metrics-driven HPA path; it is not a drop-in replacement for KEDA's
  scale-from-zero / event triggers.
- **OpenCost/Kubecost** have deeper *by-namespace / by-label allocation* and live
  billing feeds. K8-Optimizer computes per-workload cost + savings with
  per-cloud prices; the live billing-feed adapters are stubbed interfaces, not
  implemented.
- **ScaleOps** is the closest full-stack analogue. K8-Optimizer reimplements its
  core loop (observe → recommend → gate → apply, in-place resize by default);
  ScaleOps additionally has proprietary ML/forecasting and managed-node
  features that this project does not attempt to replicate.

## What "zero downtime" actually means here (and what it doesn't)

- Implemented & tested: usage-floor clamping, confidence + significance gating,
  in-place resize preference, PDB-aware rolling-update fallback, and never
  mutating a workload whose safety check vetoed the change.
- Not demonstrated here: a live production cluster with real traffic failing
  over during an applied resize. That is the standard every vendor's marketing
  claims and few open-source repos prove; this repo's falsifier suite proves the
  *logic*, not the production outcome.

## Where the gaps are

These are the honest next investments before any "best-in-class" claim is
defensible, in rough priority order:

1. **Live cluster integration** — replace the fake API with the real
   `kubernetes` client (or the stdlib HTTP client against the in-cluster API)
   and run against a staging cluster.
2. **Live metrics ingestion** — Metrics Server / Prometheus adapter behind
   `MetricsSource`.
3. **Live billing feeds** — AWS CUR, GCP BigQuery, Azure Cost Exports, OCI Usage
   API behind `Provider.cost_config()`.
4. **Forecasting** — the recommender uses P95/max of observed usage, not
   predictive models of future traffic.
5. **SSO / OIDC** — auth is a local HMAC token today; no external IdP.
6. **Scale-out of the store** — SQLite today; Postgres for multi-replica
   deployments.