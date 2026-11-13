# 连锁便利店电价套餐比较与计划检修影响 API

能源采购员按门店未来 12 个月的峰谷用电预测，比较公开电价套餐的年度成本，辅助续签决策；
门店运维员依据供电公司的计划检修公告，查出会完全断电的门店与时段，提前安排应急供电。

- **Web 框架**：Python 3.11 + FastAPI（`app/main.py`）
- **核心逻辑**：仅依赖 Python 标准库（`urllib` + `html.parser` + `decimal` + `datetime`），离线可测
- **本地静态站**：随仓库提供的报价站 `quote-site/` 与计划检修公告站 `outage-site/`，nginx 容器托管，无需外网
- **部署**：Docker Compose 一键启动 API + 两个静态站

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由：套餐比较 + 检修影响
│   ├── schemas.py     # 请求/响应 Pydantic 模型
│   ├── scraper.py     # 报价站分页列表 + 套餐详情抓取解析
│   ├── outage.py      # 检修公告目录/详情抓取、区间合并、全断电计算
│   ├── fetcher.py     # http/https/file 统一抓取层与错误归一化
│   ├── html_dom.py    # 标准库实现的轻量 HTML DOM
│   └── billing.py     # 峰谷逐档计费、资格校验、年度汇总
├── quote-site/        # 本地报价站（静态 HTML，虚构演示数据）
│   ├── index.html         # 列表第 1 页（P001-P004，含 next 分页链接）
│   ├── index-2.html       # 列表第 2 页（P005-P007）
│   ├── plans/p001..p006.html
│   ├── 404.html           # p007.html 故意缺失，演示详情抓取失败
│   └── nginx.conf
├── outage-site/       # 本地计划检修公告站（静态 HTML，虚构演示数据）
│   ├── index.html         # 目录第 1 页（A-2026-001..003，含 next 分页链接）
│   ├── index-2.html       # 目录第 2 页（A-2026-004）
│   ├── announcements/a2026-001..003.html
│   ├── 404.html           # a2026-004.html 故意缺失，演示公告详情抓取失败
│   └── nginx.conf
├── tests/             # 标准库 unittest 离线测试
├── demo/              # request.json（套餐比较）、outage-request.json（检修影响）及演示脚本
├── Dockerfile
└── docker-compose.yml
```

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

- 报价站：http://localhost:8080/index.html
- 检修公告站：http://localhost:8081/index.html
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

发起比较请求（容器内通过 `http://quote-site/index.html` 访问报价站）：

```bash
curl -X POST http://localhost:8000/api/v1/compare-plans \
  -H "Content-Type: application/json" \
  --data @demo/request.json
```

发起检修影响查询（容器内通过 `http://outage-site/index.html` 访问公告站）：

```bash
curl -X POST http://localhost:8000/api/v1/maintenance-impact \
  -H "Content-Type: application/json" \
  --data @demo/outage-request.json
```

## 无 Docker 的离线运行

核心抓取/计费/检修计算逻辑仅用标准库，可直接运行测试：

```bash
python3 -I -m unittest discover -s tests -t . -v
```

手动启动静态站与 API（需自行安装 `requirements.txt`）：

```bash
python3 -m http.server 8080 --directory quote-site
python3 -m http.server 8081 --directory outage-site
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 请求中将 list_url / catalog_url 改为 http://127.0.0.1:8080/index.html、
# http://127.0.0.1:8081/index.html；也支持 file:// 绝对路径，完全离线：
#   "list_url": "file:///workspace/quote-site/index.html"
#   "catalog_url": "file:///workspace/outage-site/index.html"
# 或直接运行 demo/run_demo.sh、demo/run_outage_demo.sh
```

## 套餐比较：请求格式

`POST /api/v1/compare-plans`

| 字段 | 说明 |
| --- | --- |
| `list_url` | 报价站套餐列表页 URL，支持 `http(s)://` 与 `file://` |
| `region` | 门店地区，需与套餐详情页地区标签精确匹配（如 `上海`） |
| `monthly_usage` | 恰好 12 条记录，每条含 `month`(1-12)、`peak_kwh`、`valley_kwh`（非负） |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

## 计费与筛选规则

1. **地区过滤**：套餐适用地区不包含请求地区 → 排除（`region_not_applicable`）。
2. **费率完整性**：缺峰段或谷段费率表、费率档位无法解析、月基本费缺失 → 排除
   （`missing_rates` / `invalid_tier_structure` / `missing_base_fee`）。
3. **档位覆盖校验**：任一月峰或谷用电量超过该费率表已公布的最高档位上限
   → 排除（`usage_exceeds_published_tiers`，响应中注明月份与段别）。无上限档（止为空）可承接任意电量。
4. **逐档计费**：每月峰、谷电量分别沿各自费率表按 `[下限, 上限)` 区间逐档计费，
   金额用 `Decimal` 保留两位小数（四舍五入），再加固定月基本费得月合计。
5. **年度汇总与排序**：12 个月月合计求和为年度成本，按从低到高排序返回。

## 抓取失败处理

- 列表页（含分页）或套餐详情抓取失败（连接错误、超时、HTTP 4xx/5xx、结构无法解析）时，
  在响应 `failures` 中列出失败地址、阶段（`list`/`detail`）、原因与 HTTP 状态码。
- 只要存在任何抓取失败，`conclusion.verification_status` 标记为 `pending_verification`（待核实）；
  全部成功且存在有效套餐时为 `verified`。无有效套餐时同样为 `pending_verification`。
- 分页跟随 `a[rel=next]`，并设有 50 页安全上限与去重，避免异常站点造成无限抓取。

## 演示数据覆盖的场景

| 套餐 | 地区 | 演示中的处理结果 |
| --- | --- | --- |
| P001 安能 峰谷A | 上海/江苏/浙江/华东 | 有效，参与排序 |
| P002 安能 峰谷B | 上海/华东 | 有效，参与排序 |
| P003 沪电 阶梯标准版 | 上海 | 有效，示例数据下年度最低价 |
| P004 通商 华北套餐 | 北京/天津/华北 | 地区不匹配，排除 |
| P005 安能 A+ | 上海/江苏/华东 | 缺谷段费率，排除 |
| P006 沪电 封顶套餐 | 上海 | 第 1 月峰用电即超 150 kWh 封顶档，排除 |
| P007 安能 C | 上海/华东 | 详情页 404，进入 `failures`，最低价标为待核实 |

示例请求下的复核年度成本（元）：**P003 6899.40 < P001 7247.40 < P002 7842.00**。

## 响应结构（摘要）

```json
{
  "conclusion": {
    "lowest_plan_id": "P003",
    "lowest_plan_name": "沪电零售 · 峰谷阶梯标准版",
    "lowest_annual_total_cny": 6899.4,
    "verification_status": "pending_verification",
    "note": "存在抓取失败的列表页或套餐详情，最低价结论待核实"
  },
  "ranked_plans": [ { "rank": 1, "annual_total_cny": 6899.4,
                      "monthly_bills": [ { "peak_charges": [...], "valley_charges": [...] } ] } ],
  "excluded_plans": [ { "plan_id": "P006", "reason": "usage_exceeds_published_tiers", "detail": "..." } ],
  "failures": [ { "url": ".../p007.html", "stage": "detail", "reason": "HTTP 404", "status": 404 } ]
}
```

每个参与排序的套餐均带 `detail_url`（详情来源）与 `list_url`（发现该套餐的列表页），
月度账单逐档列出 charged_kwh 与 charge_cny，便于采购员复核。

## 计划检修影响：请求格式

`POST /api/v1/maintenance-impact`

| 字段 | 说明 |
| --- | --- |
| `catalog_url` | 计划检修公告目录页 URL，支持 `http(s)://` 与 `file://` |
| `window_start` / `window_end` | 查询时段，ISO-8601 且必须带时区偏移（如 `2026-11-14T00:00:00+08:00`）；区间为 `[start, end)`，`end` 必须晚于 `start` |
| `stores` | 门店数组，每项含 `store_id`（唯一）、可选 `store_name`、`active_lines`（该店在用线路编号，至少一条） |
| `timezone_offset` | 可选，响应时间展示用 UTC 偏移，默认 `+08:00` |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

## 检修影响计算规则

1. **目录与详情抓取**：跟随目录页 `a[rel=next]` 抓取全部分页（50 页上限、地址去重）；
   每条公告卡片的 `data-announcement-id`、`data-line-id`（或 `data-line-ids`）为目录标注的线路，
   详情页 `data-outage-window` 携带 `data-start-iso` / `data-end-iso`（必须带时区偏移）。
   目录标注线路在详情中缺失、时间缺少偏移或结束不晚于开始，均按详情失败处理。
2. **绝对时间**：不同公告可能使用不同时区偏移（如 `+09:00`），所有时间统一换算为绝对时间比较，
   跨午夜按实际经过时长处理；响应按 `timezone_offset` 展示。
3. **区间合并**：同一线路的重叠或首尾相接（结束 == 下一段开始）停电段合并为一段；有停电空档则不合并。
   查询时段外的部分会被裁剪到时段边界。
4. **全断电判定**：只有门店**全部在用线路**在同一时间片内都有停电记录，才计为全断电区间。
5. **分段列明依据**：全断电区间在公告来源变化处切分为 `segments`，每段按线路列出 `evidence`
   （`announcement_id`、`title`、`catalog_url`、`detail_url`）；区间级 `sources` 汇总全部依据。
   单线路 `line_outages[].intervals` 同样按来源变化分段。
6. **核实状态**：
   - `confirmed_full_outage`：存在已确认的全断电区间（`is_safe=false`）；
   - `confirmed_safe`：目录与涉及该门店线路的公告详情全部抓取成功，且无全断电区间（`is_safe=true`）；
   - `pending_verification`：无全断电区间但目录或涉及线路的公告详情抓取失败，
     **不得判为安全**（`is_safe=false`）。
   目录任一分页失败时 `catalog.catalog_complete=false`，所有门店都不得判为安全；
   详情失败只影响使用该公告标注线路的门店。已确认的全断电区间与失败地址仍会返回。
7. **在线路标记缺失时从严**：目录卡片未标注线路的公告若详情失败，视为可能影响所有线路
   （`affects_all_lines=true`），全部门店不得判为安全。

`catalog` 摘要字段：`visited_catalog_urls`、`announcements_found`、`announcements_parsed`、
`catalog_complete`、`outage_records`、`failure_count`。顶层 `announcements` 列出目录发现的每条公告的
目录地址、详情地址与详情抓取状态；顶层 `failures` 列出全部失败地址（阶段 `catalog` / `detail`、
原因、HTTP 状态码、涉及线路）。

## 检修公告演示数据

| 公告 | 目录标注线路 | 演示中的处理结果 |
| --- | --- | --- |
| A-2026-001 | L-101、L-102 | L-101 两段重叠（09:00-12:00 与 11:30-14:00）合并为 09:00-14:00；L-102 首尾相接（10:00-12:00 + 12:00-15:00）合并为 10:00-15:00 |
| A-2026-002 | L-101、L-102、L-104 | 夜间作业跨午夜（22:00-次日 02:00 等），按实际时间计算 |
| A-2026-003 | L-101、L-103 | L-103 以 `+09:00` 表述，换算后为 12:30-16:30 `+08:00`；L-101 来源变化处切分依据 |
| A-2026-004 | L-104 | 详情页 404，进入 `failures`；使用 L-104 的门店不得判为安全 |

`demo/outage-request.json` 查询 2026-11-14 ~ 2026-11-17 的 5 家门店：

| 门店 | 在用线路 | 结果 |
| --- | --- | --- |
| S1 城东一店 | L-101、L-102 | 全断电 11-14 10:00-15:00、11-15 23:00-次日 01:00 |
| S2 城东二店 | L-101、L-103 | 全断电 11-14 12:30-15:00 |
| S3 城北三店 | L-102、L-104 | 全断电 11-15 23:30-次日 00:30；L-104 详情失败，标记待核实 |
| S4 单线店 | L-101 | 全断电 11-14 09:00-15:00、11-15 22:00-次日 02:00 |
| S5 城南四店 | L-201 | 与本次检修无关，`confirmed_safe` |

## 检修影响响应结构（摘要）

```json
{
  "catalog": { "catalog_complete": true, "announcements_found": 4, "failure_count": 1, "...": "..." },
  "announcements": [ { "announcement_id": "A-2026-004", "lines": ["L-104"],
                      "catalog_url": ".../index-2.html", "detail_url": ".../a2026-004.html",
                      "detail_status": "failed", "failure_reason": "本地文件不存在 (HTTP 404)" } ],
  "stores": [
    { "store_id": "S1", "active_lines": ["L-101", "L-102"],
      "status": "confirmed_full_outage", "is_safe": false, "verification_complete": true,
      "full_outage_intervals": [
        { "start": "2026-11-14T10:00:00+08:00", "end": "2026-11-14T15:00:00+08:00",
          "duration_minutes": 300.0, "lines": ["L-101", "L-102"],
          "segments": [
            { "start": "...", "end": "...",
              "evidence": [ { "line_id": "L-101", "sources": [ { "announcement_id": "A-2026-001",
                             "catalog_url": "...", "detail_url": "..." } ] } ] } ],
          "sources": [ { "announcement_id": "A-2026-001", "...": "..." } ] } ],
      "line_outages": [ { "line_id": "L-101", "verified": true, "intervals": [ ... ], "failures": [] } ],
      "failures": [] }
  ],
  "failures": [ { "url": ".../a2026-004.html", "stage": "detail", "status": 404,
                  "announcement_id": "A-2026-004", "lines": ["L-104"] } ]
}
```
