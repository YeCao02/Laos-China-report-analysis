from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import SNAPSHOT_END, SNAPSHOT_START, ProjectPaths
from .models import EVIDENCE_GRADES, RETRIEVAL_TIERS


def build_quality_report(conn: sqlite3.Connection, paths: ProjectPaths) -> dict:
    total = conn.execute("SELECT count(*) FROM articles").fetchone()[0]
    distinct_ids = conn.execute("SELECT count(DISTINCT record_id) FROM articles").fetchone()[0]
    source_counts = {
        row["source"]: row["n"]
        for row in conn.execute(
            "SELECT CASE WHEN upper(source_code) LIKE 'KPL%' THEN 'KPL' "
            "WHEN upper(source_code) LIKE 'PASAXON%' THEN 'PASAXON' ELSE upper(source_code) END source,"
            "count(*) n FROM articles GROUP BY source"
        )
    }
    evidence_counts = {
        row["evidence_grade"]: row["n"]
        for row in conn.execute("SELECT evidence_grade,count(*) n FROM articles GROUP BY evidence_grade")
    }
    invalid_evidence = conn.execute(
        f"SELECT count(*) FROM articles WHERE evidence_grade NOT IN ({','.join('?' for _ in EVIDENCE_GRADES)})",
        tuple(sorted(EVIDENCE_GRADES)),
    ).fetchone()[0]
    invalid_tier = conn.execute(
        f"SELECT count(*) FROM articles WHERE retrieval_tier NOT IN ({','.join('?' for _ in RETRIEVAL_TIERS)})",
        tuple(sorted(RETRIEVAL_TIERS)),
    ).fetchone()[0]
    orphan_relations = {
        table: conn.execute(
            f"SELECT count(*) FROM {table} r LEFT JOIN articles a ON a.record_id=r.{column} "
            "WHERE a.record_id IS NULL"
        ).fetchone()[0]
        for table, column in (
            ("story_clusters", "representative_record_id"),
            ("evidence_objects", "record_id"),
            ("sample_memberships", "record_id"),
            ("translation_queue", "record_id"),
        )
    }
    out_of_range = conn.execute(
        "SELECT count(*) FROM articles WHERE published_at IS NOT NULL AND "
        "(substr(published_at,1,10) < ? OR substr(published_at,1,10) > ?)",
        (SNAPSHOT_START.isoformat(), SNAPSHOT_END.isoformat()),
    ).fetchone()[0]
    missing = {
        field: conn.execute(
            f"SELECT count(*) FROM articles WHERE {field} IS NULL OR trim({field})=''"
        ).fetchone()[0]
        for field in ("title_original", "source_code", "language")
    }
    selected_rows = conn.execute(
        "SELECT a.record_id,a.original_url,a.archive_url,a.body_original,a.body_file,a.raw_file "
        "FROM articles a JOIN sample_memberships s ON s.record_id=a.record_id"
    ).fetchall()
    selected_missing_body = selected_missing_url = selected_missing_file = 0
    selected_missing_note = conn.execute(
        "SELECT count(*) FROM articles a JOIN sample_memberships s ON s.record_id=a.record_id "
        "WHERE a.china_note_zh IS NULL OR trim(a.china_note_zh)=''"
    ).fetchone()[0]
    failed_partitions = conn.execute(
        "SELECT count(*) FROM crawl_partitions WHERE status='failed'"
    ).fetchone()[0]
    sampled_cells = {
        (row["source_code"], row["year_month"]): row["n"]
        for row in conn.execute(
            "SELECT source_code,year_month,count(*) n FROM sample_memberships GROUP BY source_code,year_month"
        )
    }
    all_months: list[str] = []
    year, month = SNAPSHOT_START.year, SNAPSHOT_START.month
    while (year, month) <= (SNAPSHOT_END.year, SNAPSHOT_END.month):
        all_months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    documented_gap_cells = sum(
        sampled_cells.get((source, ym), 0) < 2
        for source in ("KPL", "PASAXON") for ym in all_months
    )
    for row in selected_rows:
        if not row["body_original"] and not row["body_file"]:
            selected_missing_body += 1
        if not row["original_url"] and not row["archive_url"]:
            selected_missing_url += 1
        for field in ("body_file", "raw_file"):
            if row[field] and not (paths.root / row[field]).exists() and not Path(row[field]).exists():
                selected_missing_file += 1

    findings: list[dict] = []

    def add(severity: str, issue: str, evidence: str, impact: str, remediation: str) -> None:
        findings.append({
            "severity": severity, "issue": issue, "evidence": evidence,
            "impact": impact, "remediation": remediation,
        })

    if total != distinct_ids:
        add("Critical", "record_id不唯一", f"rows={total}, distinct={distinct_ids}",
            "文章可能被重复计数。", "修复主键生成规则并重新幂等导入。")
    if any(orphan_relations.values()):
        add("Critical", "关系表存在孤儿行", json.dumps(orphan_relations, ensure_ascii=False),
            "聚类、证据、样本或翻译队列无法与规范文章逐条对账。",
            "仅删除外键目标不存在的关系行，随后重建聚类、样本和翻译队列。")
    if source_counts.get("PASAXON", 0) == 0:
        add("High", "Pasaxon尚未解锁", "PASAXON rows=0",
            "无法满足双来源月度目标。", "运行Pasaxon检索、标签、电子报及历史补档。")
    if selected_missing_body:
        add("High", "入选样本缺少正文或OCR", f"records={selected_missing_body}",
            "无法全文分析或进入翻译队列。", "补抓正文后重新抽样。")
    if selected_missing_url:
        add("High", "入选样本缺少可追溯URL", f"records={selected_missing_url}",
            "证据不可复核。", "补充原始或档案URL。")
    if selected_missing_file:
        add("High", "目录引用不存在", f"broken_file_references={selected_missing_file}",
            "交付物不可复现。", "重新导出或恢复原始文件。")
    if selected_missing_note:
        add("High", "入选样本缺少中文涉华说明", f"records={selected_missing_note}",
            "无法按交付字段开展内容分析。", "补充说明后重新导出。")
    if documented_gap_cells:
        add("High", "月度样本覆盖仍有真实缺口",
            f"source_month_cells_below_minimum={documented_gap_cells}/352",
            "尚不能声称2012—2026各月均达到每源至少2篇。",
            "按historical_backfill_queue.csv优先补抓2012—2020，并保留负结果审计。")
    if failed_partitions:
        add("Medium", "部分抓取分区失败", f"failed_partitions={failed_partitions}",
            "相应入口的发现完整性尚未验证。",
            "参考failed_tasks.ndjson退避重试，或改用馆藏、PDF与转载证据。")
    if out_of_range:
        add("High", "日期超出快照边界", f"records={out_of_range}",
            "污染2012—2026统计。", "隔离记录或修正日期证据。")
    if invalid_evidence or invalid_tier:
        add("High", "受控枚举字段非法", f"evidence={invalid_evidence}, tier={invalid_tier}",
            "证据过滤和抽样可能失真。", "按受控词表修复。")
    if any(missing.values()):
        add("Critical", "必填字段缺失", json.dumps(missing, ensure_ascii=False),
            "无法识别或分组记录。", "修复解析器并重新导入。")
    if not findings:
        add("Low", "未发现阻断性结构问题", "核心唯一性、必填和边界检查通过",
            "可继续用于当前阶段。", "继续补档并监控历史分区。")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_grain": "one row per source-language article version; story_id links versions",
        "intended_use": "auditable monthly research sample and full-text analysis",
        "snapshot": {"start": SNAPSHOT_START.isoformat(), "end": SNAPSHOT_END.isoformat()},
        "profile": {
            "rows": total, "distinct_record_ids": distinct_ids, "source_counts": source_counts,
            "evidence_counts": evidence_counts, "selected_records": len(selected_rows),
            "required_field_missing": missing, "documented_gap_cells": documented_gap_cells,
            "failed_partitions": failed_partitions, "orphan_relations": orphan_relations,
        },
        "checks_performed": [
            "primary-key uniqueness", "required-field completeness", "evidence/tier validity",
            "snapshot date bounds", "selected-body completeness", "selected URL traceability",
            "selected local-file referential integrity", "source coverage",
            "monthly minimum coverage", "crawl-partition failure audit",
            "orphan relationship audit",
        ],
        "findings": findings,
        "assumptions": [
            "KPL language versions remain separate records",
            "search_indexed_at is not silently treated as a verified publication date",
            "2026-08 is a partial month",
        ],
    }
    paths.audit.mkdir(parents=True, exist_ok=True)
    (paths.audit / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = [
        "# 数据质量报告", "", f"生成时间：{report['generated_at']}", "",
        "## 数据集与粒度", "", report["dataset_grain"], "",
        "## 已执行检查", "",
    ]
    md.extend(f"- {item}" for item in report["checks_performed"])
    md.extend(["", "## 发现", ""])
    for index, finding in enumerate(findings, 1):
        md.extend([
            f"### {index}. [{finding['severity']}] {finding['issue']}", "",
            f"- 证据：{finding['evidence']}", f"- 影响：{finding['impact']}",
            f"- 修复：{finding['remediation']}", "",
        ])
    md.extend(["## 假设与边界", ""] + [f"- {item}" for item in report["assumptions"]])
    (paths.audit / "quality_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return report
