"""计划检修影响计算与公告抓取的离线测试（仅依赖标准库）。

运行（在仓库根目录）：
    python3 -I -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.outage import (
    AnnouncementEntry,
    OutageRecord,
    SourceRef,
    compute_full_outage_intervals,
    crawl_outage_catalog,
    evaluate_store,
    merge_line_outages,
    parse_datetime_with_offset,
    parse_detail_page,
    parse_offset,
)

ROOT = Path(__file__).resolve().parent.parent
CATALOG_URL = (ROOT / "outage-site" / "index.html").as_uri()
TZ = timezone(timedelta(hours=8))
WINDOW_START = datetime(2026, 11, 14, 0, 0, tzinfo=TZ)
WINDOW_END = datetime(2026, 11, 17, 0, 0, tzinfo=TZ)


def _crawl():
    return crawl_outage_catalog(CATALOG_URL)


def _store(store_id: str, lines: list[str]):
    return evaluate_store(store_id, None, lines, _crawl(), WINDOW_START, WINDOW_END, TZ)


def _source(announcement_id: str = "A-TEST") -> SourceRef:
    return SourceRef(
        announcement_id=announcement_id,
        title=announcement_id,
        catalog_url="file:///catalog/index.html",
        detail_url=f"file:///announcements/{announcement_id}.html",
    )


def _record(line: str, start: str, end: str, announcement_id: str = "A-TEST") -> OutageRecord:
    return OutageRecord(
        line_id=line,
        start=parse_datetime_with_offset(start),
        end=parse_datetime_with_offset(end),
        source=_source(announcement_id),
    )


class CatalogCrawlTest(unittest.TestCase):
    def test_crawls_paginated_catalog_and_details(self) -> None:
        crawl = _crawl()
        self.assertTrue(crawl.catalog_complete)
        self.assertEqual(len(crawl.visited_catalog_urls), 2, "应跟随 next 链接抓取两页目录")
        self.assertEqual(
            [entry.announcement_id for entry in crawl.entries],
            ["A-2026-001", "A-2026-002", "A-2026-003", "A-2026-004"],
        )
        # 目录卡片标注的线路被保留下来
        ann3 = next(e for e in crawl.entries if e.announcement_id == "A-2026-003")
        self.assertEqual(ann3.lines, ["L-101", "L-103"])

        # A-2026-004 详情页故意缺失 -> 记录失败地址与涉及线路
        self.assertEqual(len(crawl.failures), 1)
        failure = crawl.failures[0]
        self.assertEqual(failure.stage, "detail")
        self.assertEqual(failure.status, 404)
        self.assertEqual(failure.lines, ["L-104"])
        self.assertEqual(failure.announcement_id, "A-2026-004")
        self.assertTrue(failure.url.endswith("a2026-004.html"))

        # 成功详情页中的停电记录带着来源地址
        self.assertEqual(len(crawl.outages), 9)
        for record in crawl.outages:
            self.assertTrue(record.source.detail_url.endswith(".html"))
            self.assertTrue(record.source.catalog_url.endswith(".html"))

    def test_catalog_failure_marks_incomplete(self) -> None:
        crawl = crawl_outage_catalog((ROOT / "outage-site" / "missing.html").as_uri())
        self.assertFalse(crawl.catalog_complete)
        self.assertEqual(len(crawl.failures), 1)
        self.assertEqual(crawl.failures[0].stage, "catalog")

    def test_detail_page_with_offset_is_parsed_in_absolute_time(self) -> None:
        entry = AnnouncementEntry(
            announcement_id="A-2026-003",
            title="补充公告",
            lines=["L-103"],
            detail_url="file:///announcements/a2026-003.html",
            catalog_url="file:///catalog/index.html",
        )
        markup = (
            '<main data-page="outage-announcement">'
            '<section data-line-id="L-103">'
            '<div data-outage-window data-start-iso="2026-11-14T13:30:00+09:00" '
            'data-end-iso="2026-11-14T17:30:00+09:00"></div>'
            "</section></main>"
        )
        records = parse_detail_page(markup, entry, entry.detail_url)
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0].start.astimezone(TZ).isoformat(),
            "2026-11-14T12:30:00+08:00",
            "+09:00 表述应换算为同一绝对时间轴",
        )
        self.assertEqual(records[0].end.astimezone(TZ).isoformat(), "2026-11-14T16:30:00+08:00")

    def test_detail_page_missing_offset_is_rejected(self) -> None:
        entry = AnnouncementEntry(
            announcement_id="A-X",
            title=None,
            lines=["L-101"],
            detail_url="file:///detail.html",
            catalog_url="file:///catalog.html",
        )
        markup = (
            '<main data-page="outage-announcement">'
            '<section data-line-id="L-101">'
            '<div data-outage-window data-start-iso="2026-11-14T09:00:00" '
            'data-end-iso="2026-11-14T12:00:00"></div>'
            "</section></main>"
        )
        with self.assertRaises(ValueError):
            parse_detail_page(markup, entry, entry.detail_url)

    def test_detail_page_missing_catalog_line_is_failure(self) -> None:
        entry = AnnouncementEntry(
            announcement_id="A-X",
            title=None,
            lines=["L-101", "L-102"],
            detail_url="file:///detail.html",
            catalog_url="file:///catalog.html",
        )
        markup = (
            '<main data-page="outage-announcement">'
            '<section data-line-id="L-101">'
            '<div data-outage-window data-start-iso="2026-11-14T09:00:00+08:00" '
            'data-end-iso="2026-11-14T12:00:00+08:00"></div>'
            "</section></main>"
        )
        with self.assertRaises(ValueError):
            parse_detail_page(markup, entry, entry.detail_url)


class MergeTest(unittest.TestCase):
    def test_overlapping_and_adjacent_segments_are_merged(self) -> None:
        records = [
            _record("L-101", "2026-11-14T09:00:00+08:00", "2026-11-14T12:00:00+08:00"),
            _record("L-101", "2026-11-14T11:30:00+08:00", "2026-11-14T14:00:00+08:00"),
            _record("L-101", "2026-11-14T14:00:00+08:00", "2026-11-14T15:00:00+08:00"),
        ]
        intervals = merge_line_outages(records, WINDOW_START, WINDOW_END, TZ)
        self.assertEqual(len(intervals), 1, "重叠与首尾相接的停电段必须合并为一段")
        self.assertEqual(intervals[0]["start"], "2026-11-14T09:00:00+08:00")
        self.assertEqual(intervals[0]["end"], "2026-11-14T15:00:00+08:00")
        self.assertEqual(intervals[0]["duration_minutes"], 360.0)

    def test_gap_keeps_separate_intervals(self) -> None:
        records = [
            _record("L-101", "2026-11-14T09:00:00+08:00", "2026-11-14T10:00:00+08:00"),
            _record("L-101", "2026-11-14T11:00:00+08:00", "2026-11-14T12:00:00+08:00"),
        ]
        intervals = merge_line_outages(records, WINDOW_START, WINDOW_END, TZ)
        self.assertEqual(len(intervals), 2, "存在停电空档时不得合并")

    def test_cross_midnight_uses_real_elapsed_time(self) -> None:
        records = [_record("L-101", "2026-11-15T22:00:00+08:00", "2026-11-16T02:00:00+08:00")]
        intervals = merge_line_outages(records, WINDOW_START, WINDOW_END, TZ)
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["start"], "2026-11-15T22:00:00+08:00")
        self.assertEqual(intervals[0]["end"], "2026-11-16T02:00:00+08:00")
        self.assertEqual(intervals[0]["duration_minutes"], 240.0)

    def test_window_clips_intervals(self) -> None:
        records = [_record("L-101", "2026-11-13T22:00:00+08:00", "2026-11-14T02:00:00+08:00")]
        intervals = merge_line_outages(records, WINDOW_START, WINDOW_END, TZ)
        self.assertEqual(intervals[0]["start"], "2026-11-14T00:00:00+08:00")
        self.assertEqual(intervals[0]["end"], "2026-11-14T02:00:00+08:00")
        self.assertEqual(intervals[0]["duration_minutes"], 120.0)

    def test_source_change_splits_segments_with_sources(self) -> None:
        records = [
            _record("L-101", "2026-11-14T09:00:00+08:00", "2026-11-14T12:00:00+08:00", "A-1"),
            _record("L-101", "2026-11-14T11:00:00+08:00", "2026-11-14T14:00:00+08:00", "A-2"),
        ]
        intervals = merge_line_outages(records, WINDOW_START, WINDOW_END, TZ)
        self.assertEqual(len(intervals), 1)
        self.assertEqual(len(intervals[0]["segments"]), 3)
        self.assertEqual(
            [s["start"] for s in intervals[0]["segments"]],
            ["2026-11-14T09:00:00+08:00", "2026-11-14T11:00:00+08:00", "2026-11-14T12:00:00+08:00"],
        )
        self.assertEqual(
            [[src["announcement_id"] for src in s["sources"]] for s in intervals[0]["segments"]],
            [["A-1"], ["A-1", "A-2"], ["A-2"]],
        )


class FullOutageTest(unittest.TestCase):
    def test_requires_all_active_lines(self) -> None:
        by_line = {
            "L-101": [_record("L-101", "2026-11-14T09:00:00+08:00", "2026-11-14T15:00:00+08:00")],
            "L-102": [_record("L-102", "2026-11-14T10:00:00+08:00", "2026-11-14T13:00:00+08:00")],
        }
        intervals = compute_full_outage_intervals(
            ["L-101", "L-102"], by_line, WINDOW_START, WINDOW_END, TZ
        )
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["start"], "2026-11-14T10:00:00+08:00")
        self.assertEqual(intervals[0]["end"], "2026-11-14T13:00:00+08:00")
        self.assertEqual(intervals[0]["lines"], ["L-101", "L-102"])

    def test_none_when_one_line_never_out(self) -> None:
        by_line = {
            "L-101": [_record("L-101", "2026-11-14T09:00:00+08:00", "2026-11-14T15:00:00+08:00")],
            "L-102": [],
        }
        self.assertEqual(
            compute_full_outage_intervals(["L-101", "L-102"], by_line, WINDOW_START, WINDOW_END, TZ),
            [],
        )

    def test_evidence_split_on_announcement_change(self) -> None:
        by_line = {
            "L-101": [
                _record("L-101", "2026-11-14T09:00:00+08:00", "2026-11-14T13:00:00+08:00", "A-1"),
                _record("L-101", "2026-11-14T13:00:00+08:00", "2026-11-14T15:00:00+08:00", "A-2"),
            ],
            "L-102": [_record("L-102", "2026-11-14T09:00:00+08:00", "2026-11-14T15:00:00+08:00", "A-1")],
        }
        intervals = compute_full_outage_intervals(
            ["L-101", "L-102"], by_line, WINDOW_START, WINDOW_END, TZ
        )
        self.assertEqual(len(intervals), 1)
        self.assertEqual(
            [segment["start"] for segment in intervals[0]["segments"]],
            ["2026-11-14T09:00:00+08:00", "2026-11-14T13:00:00+08:00"],
        )
        second = intervals[0]["segments"][1]
        evidence = {item["line_id"]: item["sources"] for item in second["evidence"]}
        self.assertEqual([s["announcement_id"] for s in evidence["L-101"]], ["A-2"])
        self.assertEqual([s["announcement_id"] for s in evidence["L-102"]], ["A-1"])
        self.assertEqual(
            sorted(s["announcement_id"] for s in intervals[0]["sources"]), ["A-1", "A-2"]
        )


class StoreEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.crawl = _crawl()

    def _evaluate(self, store_id: str, lines: list[str]):
        return evaluate_store(
            store_id, None, lines, self.crawl, WINDOW_START, WINDOW_END, TZ
        )

    def test_store_using_two_affected_lines_is_confirmed_outage(self) -> None:
        impact = self._evaluate("S1", ["L-101", "L-102"])
        self.assertEqual(impact.status, "confirmed_full_outage")
        self.assertFalse(impact.is_safe)
        self.assertTrue(impact.verification_complete)
        starts = [iv["start"] for iv in impact.full_outage_intervals]
        self.assertEqual(starts, ["2026-11-14T10:00:00+08:00", "2026-11-15T23:00:00+08:00"])
        self.assertIn(
            "请安排应急供电",
            impact.note,
        )

    def test_store_with_unrelated_line_is_confirmed_safe(self) -> None:
        impact = self._evaluate("S5", ["L-201"])
        self.assertEqual(impact.status, "confirmed_safe")
        self.assertTrue(impact.is_safe)
        self.assertEqual(impact.full_outage_intervals, [])

    def test_detail_failure_affects_only_store_using_that_line(self) -> None:
        # L-104 详情失败：涉及 L-104 的门店不得判为安全
        impacted = self._evaluate("S3", ["L-102", "L-104"])
        self.assertFalse(impacted.is_safe)
        self.assertFalse(impacted.verification_complete)
        self.assertEqual(impacted.status, "confirmed_full_outage")
        # 仍返回已确认的全断电区间
        self.assertEqual(
            [iv["start"] for iv in impacted.full_outage_intervals],
            ["2026-11-15T23:30:00+08:00"],
        )
        self.assertEqual(len(impacted.failures), 1)
        self.assertTrue(impacted.failures[0].url.endswith("a2026-004.html"))

        unaffected = self._evaluate("S1", ["L-101", "L-102"])
        self.assertTrue(unaffected.verification_complete)
        self.assertEqual(unaffected.failures, [])

    def test_catalog_failure_makes_all_stores_unsafe(self) -> None:
        crawl = crawl_outage_catalog((ROOT / "outage-site" / "missing.html").as_uri())
        for store_id, lines in (("S1", ["L-101"]), ("S2", ["L-999"])):
            impact = evaluate_store(
                store_id, None, lines, crawl, WINDOW_START, WINDOW_END, TZ
            )
            self.assertFalse(impact.is_safe, "目录抓取失败时任何门店都不得判为安全")
            self.assertEqual(impact.status, "pending_verification")
            self.assertFalse(impact.verification_complete)
            self.assertEqual(impact.full_outage_intervals, [])

    def test_partial_catalog_failure_keeps_confirmed_intervals_but_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "outage-site"
            shutil.copytree(ROOT / "outage-site", site)
            # 第 2 页分页链接失效：第 1 页公告仍可解析，但目录不完整。
            second_page = site / "index-2.html"
            second_page.unlink()
            crawl = crawl_outage_catalog((site / "index.html").as_uri())
        self.assertFalse(crawl.catalog_complete)
        impact = evaluate_store("S1", None, ["L-101", "L-102"], crawl, WINDOW_START, WINDOW_END, TZ)
        self.assertFalse(impact.is_safe)
        self.assertEqual(impact.status, "confirmed_full_outage")
        self.assertIn("待核实", impact.note)

    def test_line_outage_view_marks_unverified_line(self) -> None:
        impact = self._evaluate("S3", ["L-102", "L-104"])
        by_line = {item["line_id"]: item for item in impact.line_outages}
        self.assertTrue(by_line["L-102"]["verified"])
        self.assertFalse(by_line["L-104"]["verified"])
        self.assertEqual(len(by_line["L-104"]["failures"]), 1)

    def test_no_active_lines_yields_no_full_outage(self) -> None:
        by_line = {"L-101": [_record("L-101", "2026-11-14T09:00:00+08:00", "2026-11-14T15:00:00+08:00")]}
        self.assertEqual(
            compute_full_outage_intervals([], by_line, WINDOW_START, WINDOW_END, TZ), []
        )


class TimezoneTest(unittest.TestCase):
    def test_offsets_parse(self) -> None:
        self.assertEqual(parse_offset("+08:00"), timezone(timedelta(hours=8)))
        self.assertEqual(parse_offset("-05:00"), timezone(timedelta(hours=-5)))
        self.assertEqual(parse_offset("Z"), timezone.utc)
        self.assertEqual(parse_offset("UTC+08:00"), timezone(timedelta(hours=8)))
        with self.assertRaises(ValueError):
            parse_offset("8")

    def test_output_uses_requested_display_timezone(self) -> None:
        records = [_record("L-101", "2026-11-15T22:00:00+08:00", "2026-11-16T02:00:00+08:00")]
        intervals = merge_line_outages(records, WINDOW_START, WINDOW_END, timezone.utc)
        self.assertEqual(intervals[0]["start"], "2026-11-15T14:00:00+00:00")
        self.assertEqual(intervals[0]["end"], "2026-11-15T18:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
