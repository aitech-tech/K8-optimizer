# Research: ScaleOps + open-source Kubernetes cost optimization

## What ScaleOps does

ScaleOps is an **autonomous Kubernetes cost-optimization and resource
management** platform (scaleops.com). Its core loop:

1. **Continuously observes** actual CPU/memory usage and request throughput of
   every workload, in real time.
2. **Automatically right-sizes** CPU, memory *and* replica counts — eliminating
   the manual setting of resource requests/limits in Deployment/StatefulSet
   specs.
3. **Applies changes with zero downtime**, via in-place pod resizing rather
   than eviction/restart.
4. **Integrates natively** with AWS / GCP / Azure cost tools for FinOps
   reporting.
5. **Deploys in one Helm command** into a cluster.

Its differentiator vs. stock Kubernetes autoscaling is *hands-off, workload
behavior-driven* optimization with a *no-downtime guarantee* (stock VPA "Auto"
mode restarts pods).

## Open-source survey (what we borrowed, and why)

| Project | Stars | What it contributes | K8-Optimizer mapping |
|---|---|---|---|
| **kubernetes/autoscaler (VPA)** | core | Pxx-percentile CPU + max memory recommendation; safety margins | `recommender.py` percentile + floor logic |
| **aws/karpenter** | 7.7k+ | Node-level bin-packing / dynamic node provisioning | node consolidation is out of scope for v1; documented as follow-on |
| **kedacore/keda** | 10.5k+ | Event-driven, scale-to-zero autoscaling | complementary to our replica sizing; HPA/KEDA metrics consumed |
| **kubernetes-sigs/descheduler** | core | Pod defragmentation / bin-packing | follow-on: post-shrink re-packing |
| **kubecost / opencost** | 675+ | Cost allocation (per-namespace/workload $) | `cost.py` allocation + savings projection |
| **Fairwinds Goldilocks** | — | VPA-driven right-sizing UX | CLI/API surface inspiration |
| **doordash/elastic-cpu-pod-autoscaler** | 2 | Restart-free pod resize via KEP-1287 | `resize.py` in-place strategy |
| **KEP-1287 / KEP-3685** | — | `InPlacePodVerticalScaling`; the *no-downtime* primitive itself | the safety engine's primary strategy |

## Zero-downtime guarantees we encode

1. **In-place resize first** (`pods/resize` subresource) — no eviction/restart.
2. **Usage floor** — never size a request below observed real usage (minus a
   small buffer), so pods are never starved or OOM-killed.
3. **Confidence gate** — do not act on low-confidence / low-sample signals.
4. **Max-reduction cap** — bound any single shrink (default 90%).
5. **QoS / PDB awareness** — `Guaranteed` QoS or high-minAvailable workloads
   are forced to the in-place path, never a restarting rollout.
6. **Significance threshold** — ignore noise-level changes.

## Hugging Face / marketplace

No production-grade *custom* model is required for v1: the recommendation is a
deterministic statistical engine (percentiles), which is what the leaders
actually ship (VPA-style) rather than a data-hungry ML model. A
forecasting/reinforcement overlay could be added later; we deliberately kept
the core explainable and dependency-free. (No relevant pre-trained model on
Hugging Face Hub materially beat the percentile baseline for this task.)

## Roadmap

- [ ] Node consolidation (Karpenter-style bin-packing) engine
- [ ] descheduler-style re-packing after shrink
- [ ] Prometheus metrics-server ingestion adapter (live feed vs. static JSON)
- [ ] Helm chart
- [ ] Optional forecasting model for bursty workloads