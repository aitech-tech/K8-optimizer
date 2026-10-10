from k8optimizer.cost import CostConfig, report_workload, summarize
from k8optimizer.models import UsageSample, Workload
from k8optimizer.recommender import recommend


def _wk(cpu_req, mem_req, replicas=1):
    return Workload(name="api", namespace="prod",
                    cpu_request_millis=cpu_req, memory_request_mib=mem_req,
                    replicas=replicas,
                    usage=[UsageSample(100.0, 200.0) for _ in range(30)])


def test_baseline_cost_matches_formula():
    cfg = CostConfig(cpu_usd_per_vcpu_hour=0.04, memory_usd_per_gib_month=2.0, monthly_hours=730.0)
    w = _wk(cpu_req=1000.0, mem_req=1024.0, replicas=2)  # 1 vCPU, 1 GiB, 2 replicas
    rep = report_workload(w, cfg=cfg)
    # cpu: 1 vCPU * 2 rep * 730h * 0.04 = 58.4 ; mem: 1 GiB * 2 rep * 2.0 = 4.0
    assert abs(rep.baseline.cpu_cost - 58.4) < 0.01
    assert abs(rep.baseline.memory_cost - 4.0) < 0.01


def test_optimized_cost_lower_than_baseline():
    w = _wk(cpu_req=4000.0, mem_req=4096.0, replicas=4)
    rec = recommend(w)
    rep = report_workload(w, rec)
    assert rep.optimized.total < rep.baseline.total
    assert rep.savings > 0
    assert 0 < rep.savings_pct <= 100


def test_summary_aggregates():
    w1 = _wk(4000.0, 4096.0, 4)
    w2 = _wk(2000.0, 2048.0, 2)
    reports = [report_workload(w1, recommend(w1)), report_workload(w2, recommend(w2))]
    s = summarize(reports)
    assert s["workloads"] == 2
    assert s["savings_monthly"] > 0
    assert s["currency"] == "USD"