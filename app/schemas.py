"""API 请求 / 响应模型（Pydantic v2）。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .outage import parse_datetime_with_offset, parse_offset


class MonthlyUsage(BaseModel):
    month: int = Field(ge=1, le=12, description="月份，1-12")
    peak_kwh: float = Field(ge=0, description="当月峰段用电量（千瓦时）")
    valley_kwh: float = Field(ge=0, description="当月谷段用电量（千瓦时）")


class CompareRequest(BaseModel):
    list_url: str = Field(min_length=1, description="报价站套餐列表页 URL，支持 http/https/file")
    region: str = Field(min_length=1, description="门店所在地区，需与套餐页地区标签匹配，如“上海”")
    monthly_usage: list[MonthlyUsage] = Field(min_length=12, max_length=12, description="未来 12 个月逐月峰谷用电量")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    @field_validator("list_url")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("list_url 仅支持 http://、https:// 或 file:// 协议")
        return value

    @field_validator("monthly_usage")
    @classmethod
    def _validate_months(cls, value: list[MonthlyUsage]) -> list[MonthlyUsage]:
        months = sorted(item.month for item in value)
        if months != list(range(1, 13)):
            raise ValueError("monthly_usage 必须恰好包含 1-12 月各一条记录")
        return value


class TierChargeOut(BaseModel):
    tier_index: int
    lower_kwh: float
    upper_kwh: float | None
    rate_cny_per_kwh: float
    charged_kwh: float
    charge_cny: float


class MonthlyBillOut(BaseModel):
    month: int
    peak_kwh: float
    valley_kwh: float
    peak_charges: list[TierChargeOut]
    valley_charges: list[TierChargeOut]
    peak_energy_fee_cny: float
    valley_energy_fee_cny: float
    base_fee_cny: float
    monthly_total_cny: float


class RankedPlanOut(BaseModel):
    rank: int
    plan_id: str
    plan_name: str
    supplier: str | None
    regions: list[str]
    annual_total_cny: float
    detail_url: str
    list_url: str
    monthly_bills: list[MonthlyBillOut]


class ExcludedPlanOut(BaseModel):
    plan_id: str
    plan_name: str
    supplier: str | None
    regions: list[str]
    reason: str
    detail: str
    detail_url: str
    list_url: str


class CrawlFailureOut(BaseModel):
    url: str
    stage: str
    reason: str
    status: int | None


class CompareResponse(BaseModel):
    request_region: str
    list_url: str
    crawl: dict
    conclusion: dict
    ranked_plans: list[RankedPlanOut]
    excluded_plans: list[ExcludedPlanOut]
    failures: list[CrawlFailureOut]


# ---------------------------------------------------------------------------
# 计划检修影响 API
# ---------------------------------------------------------------------------


class StoreInput(BaseModel):
    store_id: str = Field(min_length=1, description="门店编号，需在本次请求内唯一")
    store_name: str | None = Field(default=None, description="门店名称，便于人工核对")
    active_lines: list[str] = Field(min_length=1, description="该门店当前在用的供电线路编号（如 L-101）")

    @field_validator("store_id")
    @classmethod
    def _strip_store_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("store_id 不能为空白")
        return value

    @field_validator("active_lines")
    @classmethod
    def _clean_lines(cls, value: list[str]) -> list[str]:
        cleaned = [line.strip() for line in value if line and line.strip()]
        if not cleaned:
            raise ValueError("active_lines 至少需要一条非空线路编号")
        seen: dict[str, None] = {}
        for line in cleaned:
            seen.setdefault(line, None)
        return list(seen)


class OutageImpactRequest(BaseModel):
    catalog_url: str = Field(min_length=1, description="计划检修公告目录 URL（支持 http/https/file）")
    window_start: str = Field(description="查询时段开始，ISO-8601 且必须带时区偏移，如 2026-11-14T00:00:00+08:00")
    window_end: str = Field(description="查询时段结束（不含），ISO-8601 且必须带时区偏移")
    stores: list[StoreInput] = Field(min_length=1, max_length=500, description="各门店及其在用线路")
    timezone_offset: str = Field(default="+08:00", description="响应时间展示用的 UTC 偏移，如 +08:00")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    @field_validator("catalog_url")
    @classmethod
    def _validate_catalog_scheme(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("catalog_url 仅支持 http://、https:// 或 file:// 协议")
        return value

    @field_validator("window_start")
    @classmethod
    def _validate_window_start(cls, value: str) -> str:
        parse_datetime_with_offset(value)
        return value.strip()

    @field_validator("window_end")
    @classmethod
    def _validate_window_end(cls, value: str) -> str:
        parse_datetime_with_offset(value)
        return value.strip()

    @field_validator("timezone_offset")
    @classmethod
    def _validate_timezone_offset(cls, value: str) -> str:
        parse_offset(value)
        return value.strip()

    @model_validator(mode="after")
    def _validate_window_order(self) -> "OutageImpactRequest":
        start = parse_datetime_with_offset(self.window_start)
        end = parse_datetime_with_offset(self.window_end)
        if end <= start:
            raise ValueError("window_end 必须晚于 window_start")
        store_ids = [store.store_id for store in self.stores]
        if len(store_ids) != len(set(store_ids)):
            raise ValueError("stores 中的 store_id 必须唯一")
        return self


class SourceOut(BaseModel):
    announcement_id: str
    title: str | None
    catalog_url: str
    detail_url: str


class EvidenceOut(BaseModel):
    line_id: str
    sources: list[SourceOut]


class FullOutageSegmentOut(BaseModel):
    start: str
    end: str
    duration_minutes: float
    evidence: list[EvidenceOut]


class FullOutageIntervalOut(BaseModel):
    start: str
    end: str
    duration_minutes: float
    lines: list[str]
    sources: list[SourceOut]
    segments: list[FullOutageSegmentOut]


class LineIntervalSegmentOut(BaseModel):
    start: str
    end: str
    duration_minutes: float
    sources: list[SourceOut]


class LineIntervalOut(BaseModel):
    start: str
    end: str
    duration_minutes: float
    sources: list[SourceOut]
    segments: list[LineIntervalSegmentOut]


class LineOutageOut(BaseModel):
    line_id: str
    verified: bool
    intervals: list[LineIntervalOut]
    failures: list["OutageFailureOut"]


class OutageFailureOut(BaseModel):
    url: str
    stage: str
    reason: str
    status: int | None
    announcement_id: str | None
    lines: list[str]
    affects_all_lines: bool


class AnnouncementOut(BaseModel):
    announcement_id: str
    title: str | None
    lines: list[str]
    catalog_url: str
    detail_url: str
    detail_status: str
    failure_reason: str | None


class StoreImpactOut(BaseModel):
    store_id: str
    store_name: str | None
    active_lines: list[str]
    status: str = Field(description="confirmed_full_outage / confirmed_safe / pending_verification")
    is_safe: bool = Field(description="仅当核实完整且无全断电区间时为 true")
    verification_complete: bool
    note: str
    full_outage_intervals: list[FullOutageIntervalOut]
    line_outages: list[LineOutageOut]
    failures: list[OutageFailureOut]


class OutageImpactResponse(BaseModel):
    catalog_url: str
    window_start: str
    window_end: str
    timezone_offset: str
    catalog: dict
    announcements: list[AnnouncementOut]
    stores: list[StoreImpactOut]
    failures: list[OutageFailureOut]
