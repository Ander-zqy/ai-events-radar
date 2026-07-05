# AI 活动雷达

自动聚合 AI 活动 → 飞书多维表格人工审核 → 分发到飞书社群 + 网站。

## 文件结构

```
event_scraper.py   抓取(Luma / 活动行 / 联谱 / Devpost)
normalize.py       归一成 16 字段契约结构
enrich.py          进详情页补简介 / 主办方
translate.py       LLM 补中英双语
stability.py       safe_get 重试 / 源健康哨兵 / 运行报告
sink.py            upsert 进飞书多维表格
run.py             管道入口,串起以上所有层

.github/workflows/scrape.yml   GitHub Actions 每日定时
requirements.txt               Python 依赖
```

## 快速开始

```bash
pip install -r requirements.txt

# 本地跑(不翻译)
python run.py

# 本地跑(带翻译)
OPENAI_API_KEY=sk-... python run.py
```

## 环境变量 / GitHub Secrets

| 变量 | 说明 |
|---|---|
| `OPENAI_API_KEY` | OpenAI 翻译用,没有则跳过翻译 |
| `OPENAI_MODEL` | 可选,默认 `gpt-5.4-mini` |
| `FEISHU_APP_ID` | 飞书自建应用 App ID |
| `FEISHU_APP_SECRET` | 飞书自建应用 App Secret |
| `FEISHU_APP_TOKEN` | 多维表格 app_token(URL 里读) |
| `FEISHU_TABLE_ID` | 多维表格 table_id(URL 里读) |

## 数据流

```
抓取(4源) → normalize → enrich → translate → 飞书审核台(pending)
                                                    ↓ 审核员改 status
                                              approved → 网站 / 机器人推送
```

## 审核台字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| id | 文本 | hash(source_url),去重键 |
| status | 单选 | pending / approved / rejected |
| name_zh / name_en | 文本 | 中英文活动名 |
| start_time / end_time | 日期 | 带时区 |
| city / venue | 文本 | 城市 / 具体地点 |
| is_online | 复选框 | 是否线上 |
| desc_zh / desc_en | 文本 | 中英文简介 |
| organizer | 文本 | 主办方 |
| register_url | 文本 | 报名链接 |
| source | 单选 | luma/huodongxing/lianpu/devpost |
| lang | 单选 | 原文语种 zh/en |
| topic_tags | 文本 | 逗号分隔主题标签 |
