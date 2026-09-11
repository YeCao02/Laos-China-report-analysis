from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from .config import ProjectPaths


def _family(source_code: str) -> str:
    code = source_code.upper()
    if code.startswith("KPL"):
        return "KPL"
    if code.startswith("PASAXON"):
        return "PASAXON"
    return code


def _historical_coverage(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Summarize all 108 source-month cells in the 2012--2020 priority period."""
    sample_counts = {
        (row["source_code"], row["year_month"]): row["n"]
        for row in conn.execute(
            "SELECT source_code,year_month,count(*) n FROM sample_memberships "
            "WHERE year_month BETWEEN '2012-01' AND '2020-12' "
            "GROUP BY source_code,year_month"
        )
    }
    result: dict[str, dict[str, int]] = {}
    for source in ("KPL", "PASAXON"):
        statuses: Counter[str] = Counter()
        for year in range(2012, 2021):
            for month in range(1, 13):
                count = sample_counts.get((source, f"{year:04d}-{month:02d}"), 0)
                status = "target_met" if count >= 4 else "minimum_met" if count >= 2 else "documented_gap"
                statuses[status] += 1
        result[source] = dict(statuses)
    return result


def export_source_year_inventory(conn: sqlite3.Connection, paths: ProjectPaths) -> list[dict]:
    rows = conn.execute(
        "SELECT source_code,substr(published_at,1,4) year,count(*) records,"
        "sum(body_original IS NOT NULL AND trim(body_original)<>'') bodies,"
        "count(DISTINCT CASE WHEN body_original IS NOT NULL AND trim(body_original)<>'' "
        "THEN substr(published_at,1,7) END) body_months,"
        "count(DISTINCT substr(published_at,1,7)) candidate_months,"
        "sum(evidence_grade='A1') a1,sum(evidence_grade='A2') a2,"
        "sum(evidence_grade='B1') b1,sum(evidence_grade='B2') b2,"
        "sum(evidence_grade='C1') c1,sum(evidence_grade='C2') c2,"
        "min(substr(published_at,1,10)) first_date,max(substr(published_at,1,10)) last_date "
        "FROM articles WHERE published_at IS NOT NULL GROUP BY source_code,year "
        "ORDER BY source_code,year"
    ).fetchall()
    selected = {
        (row["source_code"], row["year"]): row["n"]
        for row in conn.execute(
            "SELECT a.source_code,substr(a.published_at,1,4) year,count(*) n "
            "FROM articles a JOIN sample_memberships s ON s.record_id=a.record_id "
            "GROUP BY a.source_code,year"
        )
    }
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        item["source_family"] = _family(item["source_code"])
        item["selected"] = selected.get((item["source_code"], item["year"]), 0)
        result.append(item)
    fields = (
        "source_family", "source_code", "year", "records", "bodies", "candidate_months",
        "body_months", "selected", "a1", "a2", "b1", "b2", "c1", "c2",
        "first_date", "last_date",
    )
    with (paths.catalog / "source_year_inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(result)

    family_year: dict[tuple[str, str], Counter[str]] = {}
    for item in result:
        key = (item["source_family"], item["year"])
        totals = family_year.setdefault(key, Counter())
        for field in ("records", "bodies", "candidate_months", "body_months", "selected",
                      "a1", "a2", "b1", "b2", "c1", "c2"):
            # Month counts are recomputed below because sub-sources can overlap.
            if field not in {"candidate_months", "body_months"}:
                totals[field] += int(item[field] or 0)
    month_rows = conn.execute(
        "SELECT CASE WHEN upper(source_code) LIKE 'KPL%' THEN 'KPL' "
        "WHEN upper(source_code) LIKE 'PASAXON%' THEN 'PASAXON' ELSE upper(source_code) END family,"
        "substr(published_at,1,4) year,count(DISTINCT substr(published_at,1,7)) candidate_months,"
        "count(DISTINCT CASE WHEN body_original IS NOT NULL AND trim(body_original)<>'' "
        "THEN substr(published_at,1,7) END) body_months,"
        "min(substr(published_at,1,10)) first_date,max(substr(published_at,1,10)) last_date "
        "FROM articles WHERE published_at IS NOT NULL GROUP BY family,year"
    ).fetchall()
    family_year_rows = []
    for row in month_rows:
        totals = family_year[(row["family"], row["year"])]
        family_year_rows.append({
            "source_family": row["family"], "year": row["year"],
            **{field: totals[field] for field in ("records", "bodies", "selected", "a1", "a2", "b1", "b2", "c1", "c2")},
            "candidate_months": row["candidate_months"], "body_months": row["body_months"],
            "first_date": row["first_date"], "last_date": row["last_date"],
        })
    family_fields = (
        "source_family", "year", "records", "bodies", "candidate_months", "body_months",
        "selected", "a1", "a2", "b1", "b2", "c1", "c2", "first_date", "last_date",
    )
    with (paths.catalog / "source_family_year_inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=family_fields)
        writer.writeheader()
        writer.writerows(family_year_rows)

    family_rows = conn.execute(
        "SELECT CASE WHEN upper(source_code) LIKE 'KPL%' THEN 'KPL' "
        "WHEN upper(source_code) LIKE 'PASAXON%' THEN 'PASAXON' ELSE upper(source_code) END family,"
        "count(*) records,sum(body_original IS NOT NULL AND trim(body_original)<>'') bodies,"
        "count(DISTINCT CASE WHEN body_original IS NOT NULL AND trim(body_original)<>'' "
        "THEN substr(published_at,1,7) END) body_months,"
        "min(substr(published_at,1,10)) first_date,max(substr(published_at,1,10)) last_date "
        "FROM articles GROUP BY family ORDER BY family"
    ).fetchall()
    coverage = {
        row["coverage_status"]: row["n"]
        for row in conn.execute(
            "SELECT CASE WHEN n>=4 THEN 'target_met' WHEN n>=2 THEN 'minimum_met' "
            "ELSE 'documented_gap' END coverage_status,count(*) n FROM ("
            "SELECT source_code,year_month,count(*) n FROM sample_memberships "
            "GROUP BY source_code,year_month) GROUP BY coverage_status"
        )
    }
    historical_coverage = _historical_coverage(conn)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "grain": "one row per source-code and publication year",
        "families": [dict(row) for row in family_rows],
        "coverage_status_present_cells": coverage,
        "historical_coverage_2012_2020": historical_coverage,
        "rows": result,
    }
    (paths.audit / "source_inventory.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = ["# 当前语料获取盘点", "", f"生成时间：{report['generated_at']}", "", "## 结论", ""]
    for row in family_rows:
        md.append(
            f"- **{row['family']}**：候选语言版本 {row['records']:,} 条，已取得正文 "
            f"{row['bodies']:,} 篇，正文覆盖 {row['body_months']} 个月；日期范围 "
            f"{row['first_date']} 至 {row['last_date']}。"
        )
    md.extend([
        "", "候选数量不等于可分析全文数量；月度配额只由有正文、证据合格的独立事件填充。", "",
        "## 2012—2020重点期月度覆盖", "",
        "| 信源 | 达到目标（≥4） | 达到最低（2—3） | 有据缺口（<2） | 合计 |",
        "|---|---:|---:|---:|---:|",
    ])
    for source in ("KPL", "PASAXON"):
        counts = historical_coverage[source]
        md.append(
            f"| {source} | {counts.get('target_met', 0)} | {counts.get('minimum_met', 0)} | "
            f"{counts.get('documented_gap', 0)} | 108 |"
        )
    md.extend([
        "", "KPL当前唯一的重点期有据缺口为2014-08；该月尚未发现可进入母集的记录。", "",
        "## 信源与年份明细", "",
        "| 信源 | 子源 | 年份 | 候选 | 正文 | 候选月份 | 正文月份 | 入选 | A1 | A2 | B1 | 日期范围 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for item in result:
        md.append(
            f"| {item['source_family']} | {item['source_code']} | {item['year']} | "
            f"{item['records']} | {item['bodies']} | {item['candidate_months']} | "
            f"{item['body_months']} | {item['selected']} | {item['a1']} | {item['a2']} | "
            f"{item['b1']} | {item['first_date']}—{item['last_date']} |"
        )
    md.extend([
        "", "## 证据解释", "",
        "- A1：当前官方详情页正文。",
        "- A2：官方检索或列表页元数据，正文尚未全部补抓。",
        "- B1：官方旧域经网页档案恢复的正文，保留原URL、档案URL、本地文件与哈希。",
        "- 2012—2020仍缺少的Pasaxon月份表示可访问网页档案不足或快照未命中，不表示当月没有报道。",
    ])
    (paths.audit / "source_inventory.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return result
