# Laos–China Report Analysis

面向老挝官方媒体涉华新闻的可审计采集与语料构建工具。项目目前支持老挝通讯社（KPL／Khaosan Pathet Lao）和老挝《人民报》（Pasaxon），覆盖现站检索、历史网页档案恢复、电子报/PDF解析、可选OCR、证据分级、事件聚类、月度抽样和多格式导出。

> 本仓库只发布程序、配置、测试和少量解析fixture，不发布批量新闻全文、数据库、PDF、WARC、运行日志或失败队列。新闻内容的版权归原媒体及相关权利人所有。

## 研究范围

- 时间范围：2012-01-01至2026-08-07；结束日期和增量回看窗口可在本地调整。
- 媒体来源：KPL／Khaosan Pathet Lao、Pasaxon。
- 默认抽样：每个来源每月4个独立事件；材料不足时保留真实缺口。
- 语料分层：候选母集、证据合格记录、取得正文记录、正式研究样本严格分离。
- 数据原则：不以抓取时间代替发布日期，不把搜索结果数量当作完整性证明，不把目录或低清封面冒充正文。

核心直接检索词包括：

- 老挝语：`ຈີນ`、`ສປ ຈີນ`、`ສປຈີນ`、`ລາວ-ຈີນ`、`ຈີນ-ລາວ`、`ລາວ ຈີນ`、`ຈີນ ລາວ`
- 英语：`China`、`Chinese`、`Lao-China`、`Laos-China`、`China-Laos`
- 扩展实体：习近平、中老铁路、磨丁、云南、一带一路、澜湄合作、中国企业与投资等。扩展命中需要主题复核。

## 已实现的数据源

### KPL

- 当前站搜索页、详情页和双语文章正文；
- 旧域 `kpl.net.la` 的Wayback索引与回放；
- Common Crawl历史索引发现；
- 已有KPL目录型种子数据的导入、正文补齐和事件聚类。

### Pasaxon

- 当前站 `/search/{query}.html`、`/tags/{query}.html?page=N`、`/epaper.html?page=N` 和文章详情页；
- 旧站 `conten/`、`articles/`、`index/`、`hotnews/`、`worldnews/`、`cooperation/`、`pasaxon-detail.php?p_id=...` 等路径族；
- Internet Archive/Wayback CDX及原始回放；
- Common Crawl索引与ARC/WARC精确Range恢复；
- Arquivo.pt官方旧站回放；
- 历史 `showlistpdf.php`、`pdf-detail.php`、`/pdfs/` 电子报目录和PDF恢复。

网页档案服务在这里是官方原始页面的保存渠道，并非第三方新闻来源。

## 处理流程

```text
多语关键词与历史入口
        ↓
搜索/标签/目录候选
        ↓
当前官网或档案原始对象下载
        ↓
来源专用DOM解析 / PDF原生文本 / 可选OCR
        ↓
标题、日期、正文、URL和SHA-256证据门控
        ↓
SQLite规范库
        ↓
事件聚类 → 月度抽样 → JSONL/CSV/Markdown导出 → 质量审计
```

### 证据等级

- `A1`：当前官方网站正文可核验。
- `A2`：当前站或官方目录元数据可核验，但正文尚未取得。
- `B1`：网页档案或官方电子报中恢复到正文，并完成日期、标题和哈希核验。
- `B2`：档案级元数据证据，正文不完整。
- `C1/C2`：历史线索或发现队列，不进入正式样本。

### PDF与OCR

PDF优先读取原生文字层；只有扫描PDF才进入300 DPI渲染和`lao+eng` OCR。OCR结果应保留逐页文本、坐标、置信度和人工复核状态。OCR不可用或低置信度不会被静默当作成功正文。

## 环境要求与安装

- Python 3.11及以上；
- 网络采集建议在稳定网络下分批运行；
- PDF/OCR是可选依赖；
- Windows、Linux和macOS均可运行，示例以PowerShell为主。

```powershell
git clone https://github.com/YeCao02/Laos-China-report-analysis.git
cd Laos-China-report-analysis
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

需要PDF与OCR功能时：

```powershell
python -m pip install -e ".[pdf,ocr]"
```

如果只从源码目录运行而不安装：

```powershell
$env:PYTHONPATH = "src"
python -m laos_china_corpus --help
```

## 快速开始

### 1. 初始化空项目

```powershell
laos-china-corpus init
```

也可以为所有命令指定独立工作目录：

```powershell
laos-china-corpus --root "D:\research\laos-corpus" init
```

### 2. 当前Pasaxon站点的限速探测

`crawl-pasaxon`有意限制为每个查询最多3页，适合入口验证和增量发现，不代表全站完整遍历。

```powershell
laos-china-corpus crawl-pasaxon `
  --query "ຈີນ" `
  --query "ລາວ-ຈີນ" `
  --pages 3 `
  --epaper-pages 2 `
  --max-articles 60
```

### 3. Pasaxon历史标签页

```powershell
laos-china-corpus crawl-pasaxon-history `
  --query "ຈີນ" `
  --start-page 1 `
  --max-pages 100 `
  --date-from 2012-01-01 `
  --date-to 2020-12-31
```

### 4. KPL候选正文补齐

```powershell
laos-china-corpus hydrate-kpl `
  --date-from 2015-01-01 `
  --date-to 2020-12-31 `
  --per-month 2 `
  --limit 100
```

### 5. Wayback历史恢复

先保存CDX索引，再用索引文件恢复网页：

```powershell
laos-china-corpus fetch-wayback-kpl-index --year 2012
laos-china-corpus crawl-wayback-kpl `
  --index-file data/staging/archive_ocr/wayback_kpl_2012_html.json `
  --scan-per-month 30 `
  --target-per-month 4

laos-china-corpus fetch-wayback-pasaxon-index --year 2012
laos-china-corpus crawl-wayback-pasaxon `
  --index-file data/staging/archive_ocr/wayback_pasaxon_home_2012.json `
  --scan-homepages-per-month 20 `
  --target-per-month 2
```

### 6. Common Crawl发现

```powershell
laos-china-corpus discover-commoncrawl `
  --index CC-MAIN-2014-52 `
  --domain pasaxon.org.la `
  --url-pattern "pasaxon.org.la/conten/*" `
  --match-type prefix
```

Common Crawl索引命中默认只是发现证据。只有从精确ARC/WARC字节区间恢复正文、验证发布日期、通过涉华筛选并核对哈希后，才允许升级为`B1`。

### 7. 聚类、抽样、导出与质量报告

```powershell
laos-china-corpus cluster
laos-china-corpus sample --sample-id main-2012-2026-v1 --target 4
laos-china-corpus queue-translations
laos-china-corpus export
laos-china-corpus quality
laos-china-corpus inventory
```

### 8. 30天回看式增量更新

```powershell
laos-china-corpus incremental-update `
  --as-of 2026-08-07 `
  --days 30 `
  --kpl-pages 1 `
  --pasaxon-pages 3 `
  --max-pasaxon-articles 60
```

此命令不会建立定时任务。采集完成后仍应重新执行聚类、抽样、导出和质量检查。

## 数据目录

运行后主要生成：

```text
data/
├─ corpus.sqlite3              # 规范主库
├─ raw/                        # 当前站原始响应及哈希
├─ staging/                    # 档案索引、WARC、PDF、OCR和待导入对象
├─ records/                    # 逐篇JSON
├─ text/                       # 逐篇Markdown正文
├─ catalog/
│  ├─ articles.csv            # 不含全文的主目录
│  ├─ articles.jsonl          # 含body_original全文
│  ├─ monthly_coverage.csv    # 来源×月份覆盖
│  └─ translation_queue.jsonl
└─ audit/
   ├─ quality_report.json/md
   ├─ source_inventory.json/md
   └─ failed_tasks.ndjson
```

核心SQLite实体：

- `articles`：每个语言版本的元数据、正文、方法、关键词、URL和哈希；
- `story_clusters`：跨语言或同一事件关联；
- `evidence_objects`：原站、网页档案、PDF等证据；
- `crawl_partitions`：检索词、时间分区、页码和执行状态；
- `sample_memberships`：月度样本、排序和入选原因；
- `translation_queue`：待翻译正文及未来模型审计字段。

正文分析的规范入口是`articles.body_original`：

```sql
SELECT record_id, source_code, published_at, title_original,
       body_original, body_method, evidence_grade
FROM articles
WHERE body_original IS NOT NULL
  AND trim(body_original) <> '';
```

## 限速与安全边界

- 默认每域约1请求/秒；同一域名不要启动多个并行采集器。
- 遇到验证码、403、挑战页或明确访问控制时停止该入口。
- 原始响应保存抓取时间、URL和SHA-256，解析失败不会输出伪空正文。
- 可配置的最低剩余磁盘空间为100 GiB，低于阈值时停止新增下载。
- `--force`会重新请求已缓存对象，只应在确认需要刷新时使用。
- 请遵守目标网站服务条款、robots规则、版权要求和所在地法律。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖KPL/Pasaxon列表页和文章页解析、Wayback/Common Crawl、旧PHP模板污染清理、老挝文Unicode、日期与URL、电子报原生PDF文本、OCR失败状态、事件聚类、抽样和导出一致性。

## 仓库结构

```text
src/laos_china_corpus/
├─ adapters/       # KPL、Pasaxon当前站DOM适配器
├─ archives/       # Wayback、Common Crawl、Arquivo.pt、PDF/OCR恢复
├─ acquire.py      # 限速采集与正文导入
├─ db.py           # SQLite Schema
├─ sampling.py     # 月度确定性抽样
├─ exporter.py     # CSV/JSONL/逐篇文件导出
├─ quality.py      # 数据质量检查
└─ cli.py          # 命令行入口

scripts/           # 专项现站采集、历史探测与外部OCR入口
tests/             # 单元测试和最小解析fixture
config/            # 来源与关键词配置参考
```

## 当前已知局限

- 历史网页档案存在强烈保存偏差，检索结果不能视为媒体真实发稿总量。
- Pasaxon当前站的小规模探测器最多遍历每个检索词3页；完整站点审计需另行分区。
- Pasaxon旧PHP文章ID本身不含日期，必须与列表页或正文日期证据连接，不能使用抓取时间代替。
- 低清封面和只有目录的电子报不升级为正文。
- `content_origin`需要进一步人工或规则重编码，不能直接用于精确统计原创和转载比例。
- 全文翻译默认不执行，翻译队列保持待决定状态。

## 已完成研究快照（数据不在本仓库）

截至2026-09-03，本地研究快照包含9,247条语言版本记录和751篇正文：KPL 9,015条/525篇正文，Pasaxon 232条/226篇正文。Pasaxon的232条是“成功发现或恢复的报道”，不是该报完整发稿量。该统计仅用于说明代码已经处理过的数据规模，不表示仓库内包含这些语料。

## 数据与版权

程序代码可以公开复现采集与审计流程；新闻HTML、PDF、OCR/提取正文和数据库不随代码仓库发布。使用者应自行评估抓取权限、合理使用、研究伦理与再分发边界。若公开研究成果，建议发布元数据、分析方法、派生统计和必要的合规短引文，而非批量转载新闻全文。

## 引用

如将本工具用于论文或数据产品，请引用本GitHub仓库和所使用的媒体原始URL/网页档案URL，并记录提交SHA、采集日期、查询词、证据等级与数据快照截止日期。
