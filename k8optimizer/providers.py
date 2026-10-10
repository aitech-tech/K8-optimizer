"""Cloud-provider adapters — one engine, every cloud.

K8-Optimizer is cloud-agnostic: the optimization engine works identically on
AWS, GCP, Azure, Oracle Cloud (OCI) and on-prem. This module provides:

* **Pricing adapters** — map per-cloud node/instance pricing into the
  ``CostConfig`` (dollars per vCPU-hour and per GiB-month) the cost engine
  already consumes, so savings projections are cloud-accurate.
* **A provider registry** with metadata (display name, region list, compute
  SKU shape) the GUI uses to render the multi-cloud dashboard.

Every adapter is a pure value + a normalization function — no SDK calls, so it
works offline and is trivially testable. Real cloud billing integration
(Cur / BigQuery / Azure Cost Exports / OCI usage API) plugs into the same
interface with the provider SDK later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .cost import CostConfig


@dataclass(frozen=True)
class Region:
    name: str
    label: str


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    kind: str  # "public" | "onprem"
    regions: List[Region] = field(default_factory=list)
    # Default on-demand commodity price used when no live billing feed exists.
    default_cpu_usd_per_vcpu_hour: float = 0.04
    default_memory_usd_per_gib_month: float = 2.0

    def cost_config(self, cpu_usd: Optional[float] = None,
                    mem_usd: Optional[float] = None, currency: str = "USD") -> CostConfig:
        return CostConfig(
            cpu_usd_per_vcpu_hour=(cpu_usd if cpu_usd is not None
                                   else self.default_cpu_usd_per_vcpu_hour),
            memory_usd_per_gib_month=(mem_usd if mem_usd is not None
                                      else self.default_memory_usd_per_gib_month),
            currency=currency,
        )


AWS = Provider(
    id="aws", name="Amazon Web Services", kind="public",
    regions=[
        Region("us-east-1", "US East (N. Virginia)"),
        Region("us-west-2", "US West (Oregon)"),
        Region("eu-west-1", "Europe (Ireland)"),
        Region("ap-south-1", "Asia Pacific (Mumbai)"),
        Region("ap-southeast-1", "Asia Pacific (Singapore)"),
    ],
    default_cpu_usd_per_vcpu_hour=0.0416,
    default_memory_usd_per_gib_month=2.10,
)

GCP = Provider(
    id="gcp", name="Google Cloud Platform", kind="public",
    regions=[
        Region("us-central1", "Iowa"),
        Region("us-east1", "South Carolina"),
        Region("europe-west1", "Belgium"),
        Region("asia-south1", "Mumbai"),
        Region("asia-southeast1", "Singapore"),
    ],
    default_cpu_usd_per_vcpu_hour=0.0400,
    default_memory_usd_per_gib_month=2.00,
)

AZURE = Provider(
    id="azure", name="Microsoft Azure", kind="public",
    regions=[
        Region("eastus", "East US"),
        Region("westus2", "West US 2"),
        Region("northeurope", "North Europe"),
        Region("centralindia", "Central India"),
        Region("southeastasia", "Southeast Asia"),
    ],
    default_cpu_usd_per_vcpu_hour=0.0430,
    default_memory_usd_per_gib_month=2.20,
)

OCI = Provider(
    id="oci", name="Oracle Cloud Infrastructure", kind="public",
    regions=[
        Region("us-ashburn-1", "Ashburn"),
        Region("us-phoenix-1", "Phoenix"),
        Region("eu-frankfurt-1", "Frankfurt"),
        Region("ap-mumbai-1", "Mumbai"),
        Region("ap-singapore-1", "Singapore"),
    ],
    default_cpu_usd_per_vcpu_hour=0.0250,
    default_memory_usd_per_gib_month=1.50,
)

ONPREM = Provider(
    id="onprem", name="On-Premises / Private Cloud", kind="onprem",
    regions=[Region("onprem", "Own datacenter")],
    # On-prem: use fully-burdened internal cost of a vCPU-hour / GiB-month.
    default_cpu_usd_per_vcpu_hour=0.0150,
    default_memory_usd_per_gib_month=1.00,
)

PROVIDERS: Dict[str, Provider] = {
    p.id: p for p in (AWS, GCP, AZURE, OCI, ONPREM)
}


def get_provider(provider_id: str) -> Provider:
    if provider_id not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider_id}")
    return PROVIDERS[provider_id]


def list_providers() -> List[dict]:
    return [
        {"id": p.id, "name": p.name, "kind": p.kind,
         "regions": [r.__dict__ for r in p.regions],
         "default_cpu_usd_per_vcpu_hour": p.default_cpu_usd_per_vcpu_hour,
         "default_memory_usd_per_gib_month": p.default_memory_usd_per_gib_month}
        for p in PROVIDERS.values()
    ]