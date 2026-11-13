"""计划检修公告抓取与“全断电”区间计算（仅依赖标准库）。

数据流：
1. 抓取分页目录（catalog），目录卡片标注受影响线路并链接到公告详情；
2. 抓取公告详情，解析每条线路的停送电时间（ISO-8601，必须带时区偏移）；
3. 同一线路的重叠或首尾相接停电段先合并；
4. 门店全部在用线路同时停电的时间片才是“全断电”；
5. 全断电区间与线路停电区间在公告来源变化处切分，逐段列明依据地址。

安全语义（保守判定）：
- 目录任一分页抓取/解析失败 -> catalog_complete=False，所有门店都不得判为安全；
- 某公告详情抓取/解析失败 -> 目录中该公告标注线路的停电情况未知，
  使用这些线路的门店不得判为安全；
- 失败不会抹掉已经成功核实到的全断电区间，响应中同时返回失败地址。

时间语义：
- 所有时间按绝对时间比较（datetime 带 tzinfo），跨午夜、跨不同时区偏移均按实际
  经过时间处理；输出统一换算到请求指定的显示时区。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .fetcher import FetchError, fetch_url, join_url
from .html_dom import Element, parse_html

MAX_CATALOG_PAGES = 50  # 分页安全上限


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """一条停电记录的公告来源（保留目录页与详情页地址）。"""

    announcement_id: str
    title: str | None
    catalog_url: str
    detail_url: str


@dataclass
class AnnouncementEntry:
    """目录中发现的公告；线路来自目录卡片的标注。"""

    announcement_id: str
    title: str | None
    lines: list[str]
    detail_url: str
    catalog_url: str

    def source(self) -> SourceRef:
        return SourceRef(
            announcement_id=self.announcement_id,
            title=self.title,
            catalog_url=self.catalog_url,
            detail_url=self.detail_url,
        )


@dataclass(frozen=True)
class OutageRecord:
    """详情页解析出的一条线路停电记录（时间均为带时区的绝对时间）。"""

    line_id: str
    start: datetime
    end: datetime
    source: SourceRef


@dataclass
class OutageFailure:
    url: str
    stage: str  # catalog | detail
    reason: str
    status: int | None = None
    announcement_id: str | None = None
    lines: list[str] = field(default_factory=list)
    # 无法确定失败公告涉及哪些线路时置 True，按“影响所有门店”处理。
    affects_all_lines: bool = False


@dataclass
class CatalogCrawlResult:
    entries: list[AnnouncementEntry] = field(default_factory=list)
    outages: list[OutageRecord] = field(default_factory=list)
    failures: list[OutageFailure] = field(default_factory=list)
    visited_catalog_urls: list[str] = field(default_factory=list)
    catalog_complete: bool = True


@dataclass
class StoreImpact:
    store_id: str
    store_name: str | None
    active_lines: list[str]
    status: str  # confirmed_full_outage | confirmed_safe | pending_verification
    is_safe: bool
    verification_complete: bool
    note: str
    full_outage_intervals: list[dict]
    line_outages: list[dict]
    failures: list[OutageFailure]


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _unique_lines(values: Iterable[str | None]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        line = _clean(value)
        if line and line not in seen:
            seen[line] = None
    return list(seen)


def parse_datetime_with_offset(value: str | None) -> datetime:
    """解析 ISO-8601 时间，要求带时区偏移（如 +08:00 / Z）。"""
    if value is None or not str(value).strip():
        raise ValueError("时间为空")
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"时间格式无法解析: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间缺少时区偏移: {value}")
    return parsed


def parse_offset(value: str | None) -> timezone:
    """解析固定 UTC 偏移（+08:00 / -05:00 / UTC / Z），供展示时区使用。"""
    if value is None or not str(value).strip():
        raise ValueError("时区偏移为空")
    text = str(value).strip()
    if text.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    if text.upper().startswith("UTC"):
        text = text[3:].strip()
    if not text.startswith(("+", "-")):
        raise ValueError(f"时区偏移格式无效（需如 +08:00）: {value}")
    sign = -1 if text.startswith("-") else 1
    text = text[1:]
    if ":" in text:
        hours_text, _, minutes_text = text.partition(":")
    elif len(text) == 4:
        hours_text, minutes_text = text[:2], text[2:]
    else:
        hours_text, minutes_text = text, "0"
    try:
        hours, minutes = int(hours_text), int(minutes_text)
    except ValueError as exc:
        raise ValueError(f"时区偏移格式无效（需如 +08:00）: {value}") from exc
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"时区偏移超出范围: {value}")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _line_ids_from_card(card: Element) -> list[str]:
    values: list[str] = [node.get("data-line-id") or "" for node in card.find_all(attr="data-line-id")]
    raw = card.get("data-line-ids")
    if raw:
        normalized = raw
        for separator in ("，", ";", "；", "|", "/", "、"):
            normalized = normalized.replace(separator, ",")
        values.extend(part for part in normalized.replace(",", " ").split())
    return _unique_lines(values)


def parse_catalog_page(markup: str, page_url: str) -> tuple[list[AnnouncementEntry], str | None]:
    """解析公告目录页，返回（公告条目, 下一页 URL）。"""
    root = parse_html(markup)
    cards = root.find_all(tag="article", attr="data-announcement-id")
    if not cards:
        has_container = root.find(tag="main", attr="data-page", attr_value="announcement-list")
        if has_container is None:
            raise ValueError("目录页缺少 announcement-list 主容器或公告卡片")
        # 合法但没有公告的列表页（例如最后一页）。
        return [], None

    entries: list[AnnouncementEntry] = []
    for card in cards:
        announcement_id = _clean(card.get("data-announcement-id"))
        if not announcement_id:
            raise ValueError("公告卡片缺少 data-announcement-id")
        link = card.find(tag="a", attr="class", attr_value="announcement-link")
        if link is None:
            link = card.find(tag="a", attr="href")
        href = link.get("href") if link is not None else None
        if not href:
            raise ValueError(f"公告 {announcement_id} 缺少详情链接")
        title = link.text or card.get("data-announcement-title") or None
        entries.append(
            AnnouncementEntry(
                announcement_id=announcement_id,
                title=title,
                lines=_line_ids_from_card(card),
                detail_url=join_url(page_url, href),
                catalog_url=page_url,
            )
        )

    next_link = root.find(tag="a", attr="rel", attr_value="next")
    next_url = None
    if next_link is not None and next_link.get("href"):
        next_url = join_url(page_url, next_link.get("href"))
    return entries, next_url


def _nearest_line_id(node: Element) -> str | None:
    current: Element | None = node
    while current is not None:
        line_id = _clean(current.get("data-line-id"))
        if line_id:
            return line_id
        current = current.parent
    return None


def _first_attr(node: Element, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = node.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _time_from_node(node: Element, entry_offset: timezone | None) -> datetime | None:
    """从 <time datetime=...> 读取时间；缺失偏移时仅在页面声明了偏移时补全。"""
    raw = node.get("datetime")
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"time 元素的 datetime 无法解析: {text}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if entry_offset is None:
            raise ValueError(f"时间缺少时区偏移: {text}")
        return parsed.replace(tzinfo=entry_offset)
    return parsed


def _window_times(window: Element, entry_offset: timezone | None) -> tuple[datetime, datetime]:
    """解析一条停送电记录的开始/结束时间（要求明确时区偏移）。

    支持：
    - data-start-iso / data-end-iso（或常见别名）直接给出带偏移的 ISO-8601；
    - 记录内两个 <time datetime="..."> 元素（第一个开始、第二个结束）。
    """
    start_raw = _first_attr(window, ("data-start-iso", "data-start", "data-outage-start"))
    end_raw = _first_attr(window, ("data-end-iso", "data-end", "data-outage-end"))
    if start_raw is not None and end_raw is not None:
        return parse_datetime_with_offset(start_raw), parse_datetime_with_offset(end_raw)

    time_nodes = window.find_all(tag="time", attr="datetime")
    if len(time_nodes) >= 2:
        start = _time_from_node(time_nodes[0], entry_offset)
        end = _time_from_node(time_nodes[-1], entry_offset)
        if start is not None and end is not None:
            return start, end
    raise ValueError("停电记录缺少带时区偏移的开始/结束时间")


def _entry_offset(container: Element) -> timezone | None:
    """公告自行声明的固定偏移（data-tz-offset），未声明返回 None。"""
    raw = container.get("data-tz-offset") or container.get("data-timezone-offset")
    return parse_offset(raw) if raw else None


def parse_detail_page(markup: str, entry: AnnouncementEntry, detail_url: str) -> list[OutageRecord]:
    """解析公告详情页；结构不完整或时间非法时抛出 ValueError。

    页面约定（见随仓库提供的 outage-site）：
    - main[data-page=outage-announcement]
    - section[data-line-id] 标记线路
    - [data-outage-window] 标记停送电记录，时间放在
      data-start-iso / data-end-iso（均须带时区偏移）
    """
    root = parse_html(markup)
    main = root.find(tag="main", attr="data-page", attr_value="outage-announcement")
    container = main if main is not None else root
    declared_lines = _unique_lines(
        node.get("data-line-id") for node in container.find_all(attr="data-line-id")
    )
    windows = container.find_all(attr="data-outage-window")
    if main is None and not declared_lines and not windows:
        raise ValueError("详情页缺少 outage-announcement 主容器或线路停电信息")

    entry_tz = _entry_offset(container)
    source = SourceRef(
        announcement_id=entry.announcement_id,
        title=entry.title,
        catalog_url=entry.catalog_url,
        detail_url=detail_url,
    )
    records: list[OutageRecord] = []
    seen: set[tuple[str, datetime, datetime]] = set()
    for window in windows:
        line_id = _nearest_line_id(window)
        if not line_id:
            raise ValueError("停电记录未标注所属线路（缺少 data-line-id）")
        try:
            start, end = _window_times(window, entry_tz)
        except ValueError as exc:
            raise ValueError(f"线路 {line_id} 的停电时间无效：{exc}") from exc
        if end <= start:
            raise ValueError(
                f"线路 {line_id} 的停电结束时间必须晚于开始时间："
                f"{start.isoformat()} -> {end.isoformat()}"
            )
        key = (line_id, start, end)
        if key in seen:
            continue
        seen.add(key)
        records.append(OutageRecord(line_id=line_id, start=start, end=end, source=source))

    missing = [line for line in entry.lines if line not in declared_lines]
    if missing:
        raise ValueError("详情页缺少目录标注线路的停电信息：" + "、".join(missing))
    declared_without_window = [
        line for line in entry.lines if line in declared_lines and not any(
            _nearest_line_id(window) == line for window in windows
        )
    ]
    if declared_without_window:
        raise ValueError(
            "详情页未给出线路的停送电时间：" + "、".join(declared_without_window)
        )
    return records


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def crawl_outage_catalog(catalog_url: str, timeout: float = 8.0) -> CatalogCrawlResult:
    """抓取分页目录及全部公告详情。所有抓取错误都记录，不抛异常。"""
    result = CatalogCrawlResult()
    seen_pages: set[str] = set()
    current: str | None = catalog_url

    while current and current not in seen_pages:
        if len(seen_pages) >= MAX_CATALOG_PAGES:
            result.catalog_complete = False
            result.failures.append(
                OutageFailure(
                    url=current,
                    stage="catalog",
                    reason=f"分页超过 {MAX_CATALOG_PAGES} 页安全上限，目录未完整抓取",
                )
            )
            break
        seen_pages.add(current)
        result.visited_catalog_urls.append(current)
        try:
            page = fetch_url(current, timeout=timeout)
        except FetchError as exc:
            result.catalog_complete = False
            result.failures.append(
                OutageFailure(url=current, stage="catalog", reason=exc.reason, status=exc.status)
            )
            break
        try:
            page_entries, next_url = parse_catalog_page(page.text, page.url)
        except Exception as exc:  # 结构异常按目录失败处理，避免把未知当安全
            result.catalog_complete = False
            result.failures.append(OutageFailure(url=current, stage="catalog", reason=str(exc)))
            break
        result.entries.extend(page_entries)
        if next_url is None:
            current = None
        elif next_url in seen_pages:
            result.catalog_complete = False
            result.failures.append(
                OutageFailure(
                    url=next_url,
                    stage="catalog",
                    reason="分页链接出现循环，目录未完整抓取",
                )
            )
            current = None
        else:
            current = next_url

    # 同一详情地址可能出现在多个分页：合并目录标注的线路后只抓一次。
    detail_map: dict[str, AnnouncementEntry] = {}
    for entry in result.entries:
        existing = detail_map.get(entry.detail_url)
        if existing is None:
            detail_map[entry.detail_url] = entry
        else:
            existing.lines = _unique_lines(existing.lines + entry.lines)
    result.entries = list(detail_map.values())

    for entry in result.entries:
        try:
            page = fetch_url(entry.detail_url, timeout=timeout)
        except FetchError as exc:
            result.failures.append(
                OutageFailure(
                    url=entry.detail_url,
                    stage="detail",
                    reason=exc.reason,
                    status=exc.status,
                    announcement_id=entry.announcement_id,
                    lines=list(entry.lines),
                    affects_all_lines=not entry.lines,
                )
            )
            continue
        try:
            records = parse_detail_page(page.text, entry, page.url)
        except Exception as exc:
            result.failures.append(
                OutageFailure(
                    url=entry.detail_url,
                    stage="detail",
                    reason=str(exc),
                    announcement_id=entry.announcement_id,
                    lines=list(entry.lines),
                    affects_all_lines=not entry.lines,
                )
            )
            continue
        result.outages.extend(records)

    return result


# ---------------------------------------------------------------------------
# 区间合并与全断电计算
# ---------------------------------------------------------------------------


def _source_key(source: SourceRef) -> tuple[str, str]:
    return (source.announcement_id, source.detail_url)


def _source_sort_key(source: SourceRef) -> tuple[str, str, str]:
    return (source.announcement_id, source.detail_url, source.title or "")


def _source_dict(source: SourceRef) -> dict:
    return {
        "announcement_id": source.announcement_id,
        "title": source.title,
        "catalog_url": source.catalog_url,
        "detail_url": source.detail_url,
    }


def format_dt(value: datetime, tz: timezone) -> str:
    return value.astimezone(tz).isoformat(timespec="seconds")


def _minutes(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 60.0, 2)


def _clip(records: list[OutageRecord], window_start: datetime, window_end: datetime) -> list[OutageRecord]:
    clipped: list[OutageRecord] = []
    for record in records:
        start = max(record.start, window_start)
        end = min(record.end, window_end)
        if start < end:
            clipped.append(OutageRecord(record.line_id, start, end, record.source))
    return clipped


def _portion_sources(records: list[OutageRecord], start: datetime, end: datetime) -> list[SourceRef]:
    """覆盖整个时间片 [start, end) 的来源；用于区间边界被多个段共享的情形。"""
    sources = {record.source for record in records if record.start <= start and record.end >= end}
    return sorted(sources, key=_source_sort_key)


def _boundaries(records_by_line: dict[str, list[OutageRecord]],
                window_start: datetime, window_end: datetime) -> list[datetime]:
    points = {window_start, window_end}
    for records in records_by_line.values():
        for record in records:
            if window_start <= record.start <= window_end:
                points.add(record.start)
            if window_start <= record.end <= window_end:
                points.add(record.end)
    return sorted(points)


def _collect_intervals(points: list[datetime],
                       describe: Callable[[datetime, datetime], dict | None]) -> list[dict]:
    """按相邻时间片聚合成连续区间，并在依据（describe 的 key）变化处切分。

    describe(start, end) 返回 None 表示该时间片无停电；否则返回
    {"key": 可比较的依据标识, "data": 该时间片的依据明细}。
    同一 key 的相邻时间片会合并为一个证据段，并复用首个时间片的明细，
    因此证据段不会被拼接后重新计算来源。
    """
    intervals: list[dict] = []
    current: dict | None = None
    previous_end: datetime | None = None
    for start, end in zip(points, points[1:]):
        described = describe(start, end)
        if described is None:
            current = None
            previous_end = end
            continue
        if current is None or previous_end != start:
            current = {"start": start, "end": end, "segments": []}
            intervals.append(current)
        if current["segments"] and current["segments"][-1]["key"] == described["key"]:
            current["segments"][-1]["end"] = end
        else:
            current["segments"].append(
                {"start": start, "end": end, "key": described["key"], "data": described["data"]}
            )
        current["end"] = end
        previous_end = end
    return intervals


def _line_outage_intervals(records: list[OutageRecord],
                           window_start: datetime, window_end: datetime,
                           tz: timezone) -> list[dict]:
    """同一线路：先合并重叠/首尾相接停电段，再在来源变化处切分依据段。"""
    clipped = _clip(records, window_start, window_end)
    if not clipped:
        return []
    points = _boundaries({clipped[0].line_id: clipped}, window_start, window_end)

    def describe(start: datetime, end: datetime):
        sources = _portion_sources(clipped, start, end)
        if not sources:
            return None
        return {"key": tuple(_source_key(source) for source in sources), "data": sources}

    intervals = _collect_intervals(points, describe)
    output: list[dict] = []
    for interval in intervals:
        source_map: dict[tuple[str, str], SourceRef] = {}
        segments = []
        for segment in interval["segments"]:
            for source in segment["data"]:
                source_map[_source_key(source)] = source
            segments.append(
                {
                    "start": format_dt(segment["start"], tz),
                    "end": format_dt(segment["end"], tz),
                    "duration_minutes": _minutes(segment["start"], segment["end"]),
                    "sources": [_source_dict(source) for source in segment["data"]],
                }
            )
        output.append(
            {
                "start": format_dt(interval["start"], tz),
                "end": format_dt(interval["end"], tz),
                "duration_minutes": _minutes(interval["start"], interval["end"]),
                "sources": [_source_dict(s) for s in sorted(source_map.values(), key=_source_sort_key)],
                "segments": segments,
            }
        )
    return output


def merge_line_outages(records: list[OutageRecord], window_start: datetime,
                       window_end: datetime, tz: timezone) -> list[dict]:
    """合并同一线路的停电区间（重叠或首尾相接），返回带来源依据的区间列表。"""
    return _line_outage_intervals(records, window_start, window_end, tz)


def compute_full_outage_intervals(active_lines: list[str],
                                  records_by_line: dict[str, list[OutageRecord]],
                                  window_start: datetime, window_end: datetime,
                                  tz: timezone) -> list[dict]:
    """全部在用线路同时停电的时间片才是全断电；按来源变化切分依据段。"""
    if not active_lines:
        return []
    clipped = {line: _clip(records_by_line.get(line, []), window_start, window_end)
               for line in active_lines}
    points = _boundaries(clipped, window_start, window_end)

    def describe(start: datetime, end: datetime):
        coverage = {}
        for line in active_lines:
            sources = _portion_sources(clipped[line], start, end)
            if not sources:
                return None
            coverage[line] = sources
        return {
            "key": tuple(
                (line, tuple(_source_key(source) for source in coverage[line]))
                for line in active_lines
            ),
            "data": coverage,
        }

    intervals = _collect_intervals(points, describe)
    output: list[dict] = []
    for interval in intervals:
        source_map: dict[tuple[str, str], SourceRef] = {}
        segments = []
        for segment in interval["segments"]:
            evidence = []
            for line in active_lines:
                sources = segment["data"][line]
                for source in sources:
                    source_map[_source_key(source)] = source
                evidence.append(
                    {
                        "line_id": line,
                        "sources": [_source_dict(source) for source in sources],
                    }
                )
            segments.append(
                {
                    "start": format_dt(segment["start"], tz),
                    "end": format_dt(segment["end"], tz),
                    "duration_minutes": _minutes(segment["start"], segment["end"]),
                    "evidence": evidence,
                }
            )
        output.append(
            {
                "start": format_dt(interval["start"], tz),
                "end": format_dt(interval["end"], tz),
                "duration_minutes": _minutes(interval["start"], interval["end"]),
                "lines": list(active_lines),
                "sources": [_source_dict(s) for s in sorted(source_map.values(), key=_source_sort_key)],
                "segments": segments,
            }
        )
    return output


# ---------------------------------------------------------------------------
# 门店判定
# ---------------------------------------------------------------------------


def failure_affects_lines(failure: OutageFailure, lines: list[str]) -> bool:
    if failure.stage == "catalog":
        return True
    if failure.affects_all_lines:
        return True
    return bool(set(failure.lines) & set(lines))


def failure_dict(failure: OutageFailure) -> dict:
    return {
        "url": failure.url,
        "stage": failure.stage,
        "reason": failure.reason,
        "status": failure.status,
        "announcement_id": failure.announcement_id,
        "lines": list(failure.lines),
        "affects_all_lines": failure.affects_all_lines,
    }


def evaluate_store(store_id: str,
                   store_name: str | None,
                   active_lines: list[str],
                   crawl: CatalogCrawlResult,
                   window_start: datetime,
                   window_end: datetime,
                   tz: timezone) -> StoreImpact:
    """计算单个门店的全断电区间与核实状态。"""
    lines = _unique_lines(active_lines)
    records_by_line = {
        line: [record for record in crawl.outages if record.line_id == line]
        for line in lines
    }
    relevant_failures = [
        failure for failure in crawl.failures
        if failure_affects_lines(failure, lines)
    ]
    uncertain = not crawl.catalog_complete or bool(relevant_failures)

    full_intervals = compute_full_outage_intervals(
        lines, records_by_line, window_start, window_end, tz
    )

    line_outages = []
    for line in lines:
        line_failures = [
            failure for failure in crawl.failures
            if failure_affects_lines(failure, [line])
        ]
        line_outages.append(
            {
                "line_id": line,
                "verified": not line_failures,
                "intervals": merge_line_outages(
                    records_by_line[line], window_start, window_end, tz
                ),
                "failures": [failure_dict(failure) for failure in line_failures],
            }
        )

    if full_intervals:
        status = "confirmed_full_outage"
        is_safe = False
        note = (
            "已确认查询时段内的全断电区间；但存在目录或公告详情抓取失败，可能还有未发现的停电安排，结论待核实"
            if uncertain
            else "已确认查询时段内的全断电区间，请安排应急供电"
        )
    elif uncertain:
        status = "pending_verification"
        is_safe = False
        if not crawl.catalog_complete:
            note = "公告目录未完整抓取，无法确认门店是否安全"
        else:
            affected = sorted(
                {line for failure in relevant_failures for line in failure.lines}
                | (set(lines) if any(f.affects_all_lines for f in relevant_failures) else set())
            )
            note = (
                "涉及线路（" + "、".join(affected) + "）的公告详情抓取失败，无法确认门店是否安全"
                if affected
                else "存在影响该门店线路的公告详情抓取失败，无法确认门店是否安全"
            )
    else:
        status = "confirmed_safe"
        is_safe = True
        note = "查询时段内全部在用线路均无停电记录，相关公告已全部核实"

    return StoreImpact(
        store_id=store_id,
        store_name=store_name,
        active_lines=lines,
        status=status,
        is_safe=is_safe,
        verification_complete=not uncertain,
        note=note,
        full_outage_intervals=full_intervals,
        line_outages=line_outages,
        failures=relevant_failures,
    )


def announcement_summaries(crawl: CatalogCrawlResult) -> list[dict]:
    """目录中发现的公告及其详情抓取状态（保留目录与详情来源地址）。"""
    failures_by_url = {failure.url: failure for failure in crawl.failures if failure.stage == "detail"}
    summaries = []
    for entry in crawl.entries:
        failure = failures_by_url.get(entry.detail_url)
        summaries.append(
            {
                "announcement_id": entry.announcement_id,
                "title": entry.title,
                "lines": list(entry.lines),
                "catalog_url": entry.catalog_url,
                "detail_url": entry.detail_url,
                "detail_status": "failed" if failure is not None else "ok",
                "failure_reason": failure.reason if failure is not None else None,
            }
        )
    return summaries
