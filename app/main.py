"""连锁便利店电价套餐比较 API。

入口：
- POST /api/v1/compare-plans：抓取报价站分页列表与套餐详情 -> 地区过滤 -> 资格校验
  （缺费率 / 超已公布档位不得参与）-> 峰谷逐档计费 + 月基本费 -> 年度汇总排序。
  任何列表或详情抓取失败都会记录失败地址，并把最低价结论标记为“待核实”。
- POST /api/v1/maintenance-impact：抓取计划检修公告的分页目录与详情，按门店在用线路
  计算全断电区间；目录或详情失败时相关门店不得判为安全。
"""
from __future__ import annotations

from fastapi import FastAPI

from .billing import annual_total, bill_plan, check_eligibility
from .outage import (
    announcement_summaries,
    crawl_outage_catalog,
    evaluate_store,
    failure_dict,
    parse_datetime_with_offset,
    parse_offset,
)
from .schemas import (
    CompareRequest,
    CompareResponse,
    CrawlFailureOut,
    ExcludedPlanOut,
    MonthlyBillOut,
    OutageImpactRequest,
    OutageImpactResponse,
    RankedPlanOut,
    TierChargeOut,
)
from .scraper import crawl_quote_site

app = FastAPI(
    title="连锁便利店电价套餐比较与检修影响 API",
    version="1.1.0",
    description="比较本地报价站公开电价套餐的年度成本；并依据计划检修公告判定门店全断电时段。",
)


def _tier_out(charges) -> list[TierChargeOut]:
    return [TierChargeOut(**vars(charge)) for charge in charges]


def _monthly_out(bills) -> list[MonthlyBillOut]:
    return [
        MonthlyBillOut(
            month=bill.month,
            peak_kwh=bill.peak_kwh,
            valley_kwh=bill.valley_kwh,
            peak_charges=_tier_out(bill.peak_charges),
            valley_charges=_tier_out(bill.valley_charges),
            peak_energy_fee_cny=bill.peak_energy_fee_cny,
            valley_energy_fee_cny=bill.valley_energy_fee_cny,
            base_fee_cny=bill.base_fee_cny,
            monthly_total_cny=bill.monthly_total_cny,
        )
        for bill in bills
    ]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/compare-plans", response_model=CompareResponse)
def compare_plans(request: CompareRequest) -> CompareResponse:
    usage = [item.model_dump() for item in request.monthly_usage]
    crawl_result = crawl_quote_site(request.list_url, timeout=request.timeout_seconds)

    ranked: list[RankedPlanOut] = []
    excluded: list[ExcludedPlanOut] = []

    for plan in crawl_result.plans:
        eligibility = check_eligibility(plan, request.region, usage)
        if not eligibility.eligible:
            excluded.append(
                ExcludedPlanOut(
                    plan_id=plan.plan_id,
                    plan_name=plan.name,
                    supplier=plan.supplier,
                    regions=plan.regions,
                    reason=eligibility.reason or "unknown",
                    detail=eligibility.detail or "",
                    detail_url=plan.detail_url,
                    list_url=plan.list_url,
                )
            )
            continue
        bills = bill_plan(plan, usage)
        ranked.append(
            RankedPlanOut(
                rank=0,  # 排序后统一回填
                plan_id=plan.plan_id,
                plan_name=plan.name,
                supplier=plan.supplier,
                regions=plan.regions,
                annual_total_cny=annual_total(bills),
                detail_url=plan.detail_url,
                list_url=plan.list_url,
                monthly_bills=_monthly_out(bills),
            )
        )

    ranked.sort(key=lambda item: item.annual_total_cny)
    for index, item in enumerate(ranked, start=1):
        item.rank = index

    failures = [
        CrawlFailureOut(url=f.url, stage=f.stage, reason=f.reason, status=f.status)
        for f in crawl_result.failures
    ]

    has_failure = bool(failures)
    if ranked:
        cheapest = ranked[0]
        conclusion = {
            "lowest_plan_id": cheapest.plan_id,
            "lowest_plan_name": cheapest.plan_name,
            "lowest_annual_total_cny": cheapest.annual_total_cny,
            "verification_status": "pending_verification" if has_failure else "verified",
            "note": (
                "存在抓取失败的列表页或套餐详情，最低价结论待核实"
                if has_failure
                else "全部列表页与套餐详情抓取成功，最低价结论已核实"
            ),
        }
    else:
        conclusion = {
            "lowest_plan_id": None,
            "lowest_plan_name": None,
            "lowest_annual_total_cny": None,
            "verification_status": "pending_verification",
            "note": "没有可参与比较的有效套餐，无法给出最低价结论，待核实",
        }

    return CompareResponse(
        request_region=request.region,
        list_url=request.list_url,
        crawl={
            "visited_list_urls": crawl_result.visited_list_urls,
            "plans_crawled": len(crawl_result.plans),
            "eligible_count": len(ranked),
            "excluded_count": len(excluded),
            "failure_count": len(failures),
        },
        conclusion=conclusion,
        ranked_plans=ranked,
        excluded_plans=excluded,
        failures=failures,
    )


@app.post("/api/v1/maintenance-impact", response_model=OutageImpactResponse)
def maintenance_impact(request: OutageImpactRequest) -> OutageImpactResponse:
    """按计划检修公告计算各门店的全断电区间与核实状态。"""
    window_start = parse_datetime_with_offset(request.window_start)
    window_end = parse_datetime_with_offset(request.window_end)
    tz = parse_offset(request.timezone_offset)

    crawl = crawl_outage_catalog(request.catalog_url, timeout=request.timeout_seconds)

    stores = [
        evaluate_store(
            store_id=store.store_id,
            store_name=store.store_name,
            active_lines=store.active_lines,
            crawl=crawl,
            window_start=window_start,
            window_end=window_end,
            tz=tz,
        )
        for store in request.stores
    ]

    all_failures = [failure_dict(failure) for failure in crawl.failures]
    summaries = announcement_summaries(crawl)
    return OutageImpactResponse(
        catalog_url=request.catalog_url,
        window_start=request.window_start,
        window_end=request.window_end,
        timezone_offset=request.timezone_offset,
        catalog={
            "visited_catalog_urls": crawl.visited_catalog_urls,
            "announcements_found": len(crawl.entries),
            "announcements_parsed": len(
                [item for item in summaries if item["detail_status"] == "ok"]
            ),
            "catalog_complete": crawl.catalog_complete,
            "outage_records": len(crawl.outages),
            "failure_count": len(all_failures),
        },
        announcements=summaries,
        stores=[
            {
                "store_id": store.store_id,
                "store_name": store.store_name,
                "active_lines": store.active_lines,
                "status": store.status,
                "is_safe": store.is_safe,
                "verification_complete": store.verification_complete,
                "note": store.note,
                "full_outage_intervals": store.full_outage_intervals,
                "line_outages": store.line_outages,
                "failures": [failure_dict(failure) for failure in store.failures],
            }
            for store in stores
        ],
        failures=all_failures,
    )
