from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .config import ProjectPaths


def _source_family(source_code: str) -> str:
    code = source_code.upper()
    if code.startswith("KPL"):
        return "KPL"
    if code.startswith("PASAXON"):
        return "PASAXON"
    return code


def _year_months(start_year: int = 2012, end_year: int = 2026):
    for year in range(start_year, end_year + 1):
        end_month = 8 if year == 2026 else 12
        for month in range(1, end_month + 1):
            yield year, f"{year:04d}-{month:02d}"


def build_status_report_artifact(conn: sqlite3.Connection, paths: ProjectPaths):
    """Build a readable, portable technical-status report from the canonical database."""
    generated_at = datetime.now(timezone.utc).isoformat()
    raw_years = conn.execute(
        "SELECT source_code,substr(published_at,1,4) year,count(*) records,"
        "sum(CASE WHEN body_original IS NOT NULL AND trim(body_original)<>'' THEN 1 ELSE 0 END) bodies,"
        "count(DISTINCT CASE WHEN body_original IS NOT NULL AND trim(body_original)<>'' "
        "THEN substr(published_at,1,7) END) body_months,"
        "sum(CASE WHEN evidence_grade='A1' THEN 1 ELSE 0 END) a1,"
        "sum(CASE WHEN evidence_grade='A2' THEN 1 ELSE 0 END) a2,"
        "sum(CASE WHEN evidence_grade='B1' THEN 1 ELSE 0 END) b1 "
        "FROM articles WHERE published_at IS NOT NULL GROUP BY source_code,year"
    ).fetchall()
    selected = Counter(
        (row["source_code"], row["year"])
        for row in conn.execute(
            "SELECT source_code,substr(year_month,1,4) year FROM sample_memberships"
        )
    )
    aggregate: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    body_month_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in conn.execute(
        "SELECT source_code,substr(published_at,1,4) year,substr(published_at,1,7) year_month "
        "FROM articles WHERE published_at IS NOT NULL AND body_original IS NOT NULL "
        "AND trim(body_original)<>''"
    ):
        body_month_sets[(_source_family(row["source_code"]), row["year"])].add(row["year_month"])
    for row in raw_years:
        key = (_source_family(row["source_code"]), row["year"])
        for field in ("records", "bodies", "a1", "a2", "b1"):
            aggregate[key][field] += int(row[field] or 0)
    yearly_rows = []
    for year in range(2012, 2027):
        for source in ("KPL", "PASAXON"):
            values = aggregate[(source, str(year))]
            yearly_rows.append({
                "source": source, "year": str(year), "records": values["records"],
                "bodies": values["bodies"], "body_months": len(body_month_sets[(source, str(year))]),
                "selected": selected[(source, str(year))], "a1": values["a1"],
                "a2": values["a2"], "b1": values["b1"],
            })

    sample_counts = Counter(
        (row["source_code"], row["year_month"])
        for row in conn.execute("SELECT source_code,year_month FROM sample_memberships")
    )
    coverage_by_year: dict[tuple[str, int], Counter[str]] = defaultdict(Counter)
    for year, ym in _year_months():
        for source in ("KPL", "PASAXON"):
            count = sample_counts[(source, ym)]
            status = "target_met" if count >= 4 else "minimum_met" if count >= 2 else "documented_gap"
            coverage_by_year[(source, year)][status] += 1
    coverage_rows = [{
        "source": source, "year": str(year), "target_met": values["target_met"],
        "minimum_met": values["minimum_met"], "documented_gap": values["documented_gap"],
        "months_in_scope": sum(values.values()),
    } for (source, year), values in sorted(coverage_by_year.items())]
    historical = []
    for source in ("KPL", "PASAXON"):
        totals = Counter()
        for row in coverage_rows:
            if row["source"] == source and int(row["year"]) <= 2020:
                totals.update({key: row[key] for key in ("target_met", "minimum_met", "documented_gap")})
        historical.append({"source": source, **totals, "months_in_scope": 108})

    profile = conn.execute(
        "SELECT count(*) records,"
        "sum(CASE WHEN body_original IS NOT NULL AND trim(body_original)<>'' THEN 1 ELSE 0 END) bodies,"
        "count(DISTINCT record_id) distinct_records FROM articles"
    ).fetchone()
    selected_total = conn.execute("SELECT count(*) FROM sample_memberships").fetchone()[0]
    failed_partitions = conn.execute(
        "SELECT count(*) FROM crawl_partitions WHERE status='failed'"
    ).fetchone()[0]
    documented_gaps = sum(row["documented_gap"] for row in coverage_rows)
    summary = [{
        "records": int(profile["records"] or 0), "bodies": int(profile["bodies"] or 0),
        "selected": int(selected_total), "documented_gaps": int(documented_gaps),
        "failed_partitions": int(failed_partitions),
    }]
    kpl_hist = next(row for row in historical if row["source"] == "KPL")
    pas_hist = next(row for row in historical if row["source"] == "PASAXON")

    source = {
        "id": "corpus_sqlite", "label": "规范语料数据库快照", "path": "data/corpus.sqlite3",
        "query": {
            "engine": "sqlite", "language": "sql",
            "description": "按来源族、年份、正文状态、证据等级和月度样本聚合规范库。",
            "executed_at": generated_at,
            "tables_used": ["articles", "sample_memberships", "crawl_partitions"],
            "filters": ["published_at between 2012-01-01 and 2026-08-07", "2026-08 is partial"],
            "metric_definitions": {
                "records": "articles表中的来源—语言版本记录数；双语同题保留为不同记录。",
                "bodies": "body_original为非空文本的语言版本记录数。",
                "selected": "sample_memberships中的入选事件代表记录数。",
                "documented_gap": "来源×月份入选数少于2的单元。",
            },
            "sql": "SELECT source_code, substr(published_at,1,4), count(*) FROM articles GROUP BY 1,2;",
        },
    }
    title = "老挝官方媒体涉华新闻语料获取状态"
    manifest = {
        "version": 1, "surface": "report", "title": title,
        "description": "KPL与Pasaxon在2012-01-01至2026-08-07的候选、正文、样本和历史缺口审计。",
        "generatedAt": generated_at,
        "cards": [
            {"id": "records", "description": "候选母集中的语言版本记录。", "dataset": "summary", "sourceId": "corpus_sqlite", "metrics": [{"label": "候选记录", "field": "records", "format": "number"}]},
            {"id": "bodies", "description": "已取得HTML正文或OCR文本的语言版本。", "dataset": "summary", "sourceId": "corpus_sqlite", "metrics": [{"label": "已获正文", "field": "bodies", "format": "number"}]},
            {"id": "selected", "description": "满足正文与证据条件的月度样本代表记录。", "dataset": "summary", "sourceId": "corpus_sqlite", "metrics": [{"label": "研究样本", "field": "selected", "format": "number"}]},
            {"id": "gaps", "description": "352个来源×月份单元中低于2篇的单元。", "dataset": "summary", "sourceId": "corpus_sqlite", "metrics": [{"label": "有据缺口", "field": "documented_gaps", "format": "number"}]},
        ],
        "charts": [{
            "id": "bodies_by_year", "title": "各年份已获取正文量",
            "subtitle": "2012—2026按来源统计的本地正文语言版本数；2026截至8月7日。",
            "headerMarkdown": "两条线显示正文获取进度；精确数量和证据构成见年份表。",
            "type": "line", "dataset": "yearly", "sourceId": "corpus_sqlite",
            "encodings": {
                "x": {"field": "year", "type": "ordinal", "label": "年份"},
                "y": {"field": "bodies", "type": "quantitative", "label": "正文数", "format": "number"},
                "color": {"field": "source", "type": "nominal", "label": "信源"},
                "tooltip": [
                    {"field": "records", "type": "quantitative", "label": "候选记录"},
                    {"field": "body_months", "type": "quantitative", "label": "正文覆盖月份"},
                    {"field": "selected", "type": "quantitative", "label": "入选样本"},
                ],
            },
            "yAxisTitle": "已获正文（语言版本）", "valueFormat": "number", "layout": "full",
            "surface": {"palettePolicy": "hard-two-root-cap", "paletteRoots": ["blue", "gold"]},
        }],
        "tables": [
            {"id": "historical_coverage", "title": "2012—2020重点期月度覆盖",
             "subtitle": "每个来源108个月；≥4为达到目标，2—3为达到最低，<2为有据缺口。",
             "dataset": "historical_coverage", "sourceId": "corpus_sqlite", "density": "spacious", "layout": "full",
             "columns": [
                 {"field": "source", "label": "信源", "type": "text"},
                 {"field": "target_met", "label": "达到目标", "format": "number"},
                 {"field": "minimum_met", "label": "达到最低", "format": "number"},
                 {"field": "documented_gap", "label": "有据缺口", "format": "number"},
                 {"field": "months_in_scope", "label": "合计月份", "format": "number"},
             ]},
            {"id": "yearly_inventory", "title": "来源—年份获取明细",
             "subtitle": "候选、正文、正文月份、样本与主要证据等级的精确计数。",
             "dataset": "yearly", "sourceId": "corpus_sqlite", "density": "dense", "layout": "full",
             "columns": [
                 {"field": "source", "label": "信源", "type": "text"}, {"field": "year", "label": "年份", "type": "text"},
                 {"field": "records", "label": "候选", "format": "number"}, {"field": "bodies", "label": "正文", "format": "number"},
                 {"field": "body_months", "label": "正文月份", "format": "number"}, {"field": "selected", "label": "入选", "format": "number"},
                 {"field": "a1", "label": "A1", "format": "number"}, {"field": "a2", "label": "A2", "format": "number"},
                 {"field": "b1", "label": "B1", "format": "number"},
             ]},
        ],
        "sources": [source],
        "blocks": [
            {"id": "title", "type": "markdown", "body": f"# {title}"},
            {"id": "summary_text", "type": "markdown", "sourceId": "corpus_sqlite", "body":
             f"## 技术摘要\n\n当前规范库包含 **{summary[0]['records']:,}** 条语言版本记录，其中 **{summary[0]['bodies']:,}** 条已有正文，"
             f"**{summary[0]['selected']:,}** 条进入研究样本。2012—2020重点期内，KPL仅余 **{kpl_hist['documented_gap']}** 个低于最低配额的月份；"
             f"Pasaxon仍有 **{pas_hist['documented_gap']}** 个。KPL历史恢复已经形成连续覆盖，Pasaxon仍是总体完整性的决定性缺口。"},
            {"id": "metrics", "type": "metric-strip", "cardIds": ["records", "bodies", "selected", "gaps"]},
            {"id": "finding", "type": "markdown", "sourceId": "corpus_sqlite", "body":
             "## 正文恢复覆盖KPL重点期，Pasaxon仍决定可比性\n\n图中比较按出版年份保存在本地的正文量。KPL候选母集很大，但A2元数据仍多于核验正文；Pasaxon正文数量较少且年份分布不连续。因此，新增资源应优先用于Pasaxon旧站、纸报、电子报与馆藏对象。"},
            {"id": "body_chart", "type": "chart", "chartId": "bodies_by_year", "layout": "full"},
            {"id": "coverage_interpretation", "type": "markdown", "sourceId": "corpus_sqlite", "body":
             f"## 重点期缺口不能解释为当月无报道\n\nKPL在108个重点期月份中有 {kpl_hist['target_met']} 个达到目标、{kpl_hist['minimum_met']} 个达到最低、"
             f"{kpl_hist['documented_gap']} 个有据缺口。Pasaxon分别为 {pas_hist['target_met']}、{pas_hist['minimum_met']} 和 {pas_hist['documented_gap']}。"
             "缺口只表示尚未取得足够可审计正文，不表示当月没有刊发涉华报道。"},
            {"id": "coverage_table", "type": "table", "tableId": "historical_coverage", "layout": "full"},
            {"id": "definitions", "type": "markdown", "body":
             "## 范围、粒度和指标定义\n\n统计范围为2012年1月1日至2026年8月7日，2026年8月为不完整月份。`record`是一条来源—语言版本；同一事件的老挝语和英语版本分别保留并以`story_id`关联。月度配额按独立事件计算，只有取得正文且证据为A/B级，或满足双重佐证的C1记录，才可进入样本。"},
            {"id": "yearly_table", "type": "table", "tableId": "yearly_inventory", "layout": "full"},
            {"id": "method", "type": "markdown", "body":
             "## 获取与核验方法\n\n现站正文使用来源专用DOM解析器；旧站内容保留原URL、档案URL、gzip HTML、抓取时间和SHA-256。Common Crawl候选只有在精确ARC/WARC区间恢复正文、URL日期可验证、涉华筛选通过且原始对象与正文双哈希一致后才升级为B1。PDF优先提取原生文本，扫描件进入300 DPI、老挝语与英语OCR及人工复核队列。"},
            {"id": "limitations", "type": "markdown", "sourceId": "corpus_sqlite", "body":
             f"## 局限、失败模式与稳健性\n\n当前仍有 **{summary[0]['documented_gaps']}** 个来源—月份低于最低样本量，并有 **{summary[0]['failed_partitions']}** 个失败抓取分区。"
             "主要风险来自Pasaxon网页档案稀疏、回放失败和纸报尚未充分数字化。主键唯一性、日期边界、证据等级、入选正文、URL追溯、本地文件引用和哈希均由自动检查覆盖；C2馆藏与转载线索不得填充配额。"},
            {"id": "next_steps", "type": "markdown", "body":
             "## 下一轮补档优先级\n\n1. 续跑已保存的Pasaxon Common Crawl 2018—2020索引，优先当前缺口月份。\n2. 扩展Pasaxon旧站实际文章路径并严格按URL或页内证据确认日期。\n3. 将NLA X 1274、京都CSEAS和2013年实体报语料解析到具体期号后，再请求或获取公开扫描对象。\n4. 继续补抓KPL A2正文，但不挤占Pasaxon历史恢复资源。"},
            {"id": "questions", "type": "markdown", "body":
             "## 尚待回答的问题\n\nPasaxon纸本或缩微胶卷在2012—2020各年的实际馆藏起止日期是什么？不同旧站URL代际是否存在未被主页快照引用的连续文章ID或日期路径？这些问题决定剩余缺口能否通过数字档案补齐，还是必须转向馆际复制。"},
        ],
    }
    artifact = {
        "surface": "report", "manifest": manifest,
        "snapshot": {"version": 1, "generatedAt": generated_at, "status": "ready",
                     "datasets": {"summary": summary, "yearly": yearly_rows,
                                  "historical_coverage": historical, "coverage_by_year": coverage_rows}},
        "sources": [source],
    }
    out = paths.audit / "corpus_status_artifact.json"
    out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    (paths.audit / "report_chart_map.md").write_text(
        "# 报告图表映射\n\n- 问题：两个来源的已获取正文量如何随出版年份变化？\n"
        "- 图形：双系列折线图，15个年份点，适合显示连续获取分布。\n"
        "- 字段：year、bodies、source；候选数、正文月份与样本数用于审计提示。\n"
        "- 配色：蓝/金双根上限，同时以图例和线形位置区分来源。\n",
        encoding="utf-8",
    )
    return out
