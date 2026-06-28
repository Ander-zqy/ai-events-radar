#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活动雷达 · 管道入口 run.py
============================================================
把五层串成一条命令:
    fetch(stability 包裹) → normalize → enrich → translate → 输出

  - 抓取阶段经 stability.run_source:计时、捕异常、查哨兵、记报告。
  - 按 id 去重(id = hash(source_url))。
  - 翻译仅在设了 ANTHROPIC_API_KEY 时执行(没 key 也能跑,只是不翻)。
  - 暂时输出契约结构的 CSV + JSON;飞书 sink 建好后把 write_outputs 换成 upsert 即可。
  - 退出码:有源归零或异常 → 1(让 CI 变红、触发告警)。

环境变量:
    ANTHROPIC_API_KEY   翻译用(没有则跳过翻译)
    OUTPUT_DIR          输出目录(默认当前目录)
============================================================
"""

import os
import sys
import csv
import json
import datetime
from dataclasses import asdict

from dotenv import load_dotenv
load_dotenv()   # 读取同目录下的 .env 文件

import event_scraper as es
from stability import RunReport, SourceHealth, run_source
from normalize import normalize
from enrich import enrich
from translate import translate, AnthropicTranslator

# 契约字段顺序(CSV 表头)
FIELDS = ["id", "status", "name_zh", "name_en", "start_time", "end_time",
          "city", "venue", "is_online", "desc_zh", "desc_en", "organizer",
          "register_url", "source_url", "source", "lang", "topic_tags",
          "created_at", "updated_at"]


def run_pipeline(translate_on: bool = True):
    report = RunReport()
    health = SourceHealth()

    # 1) 抓取(每个源独立,失败不影响其它源 + 哨兵)
    raw = []
    for name, fn in es.SOURCES:
        raw += run_source(name, fn, report, health)

    # 2) normalize + 按 id 去重
    seen, events = set(), []
    for e in raw:
        ev = normalize(asdict(e))
        if not ev.source_url or ev.id in seen:
            continue
        seen.add(ev.id)
        events.append(ev)
    print(f"    归一去重后 {len(events)} 条")

    # 3) enrich(进详情页补简介/主办方)
    enrich(events)

    # 4) translate(补双语)
    if translate_on and os.environ.get("ANTHROPIC_API_KEY"):
        translate(events, engine=AnthropicTranslator(), sleep=0.2)
    else:
        print("    [translate] 跳过(未设 ANTHROPIC_API_KEY)")

    health.save()
    return events, report


def write_outputs(events, outdir="."):
    os.makedirs(outdir, exist_ok=True)
    today = datetime.date.today().isoformat()
    rows = [asdict(e) for e in events]
    csv_path = os.path.join(outdir, f"events_{today}.csv")
    json_path = os.path.join(outdir, f"events_{today}.json")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return csv_path, json_path


def main():
    events, report = run_pipeline()
    # 本地调试保留 CSV 备份
    csv_path, _ = write_outputs(events, os.environ.get("OUTPUT_DIR", "."))
    print(f"\n已写出 CSV → {csv_path}")
    # 写入飞书审核台
    from sink import upsert
    upsert(events)
    print("\n" + report.summary())
    sys.exit(0 if report.ok() else 1)


if __name__ == "__main__":
    main()
