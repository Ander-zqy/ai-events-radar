#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 活动雷达 - 飞书机器人推送
============================================================
规则:
  - 平日: 推送所有 approved 活动
  - 周日: 先推送所有 approved 活动, 再附加一段周报汇总
  - 周报: 按城市分组, 同一城市内按时间排序

周报展示字段:
  - 活动名称
  - 地点
  - 报名链接
  - 主题关键词

说明:
  - 这里用飞书记录的 updated_time 作为“本周发布/审核更新”的近似依据
  - 如果以后你想严格区分“审核时间”和“最后编辑时间”, 可以再补一个 approved_at 字段
============================================================
"""

import os
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "")
TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "")
BOT_WEBHOOK = os.environ.get("FEISHU_BOT_WEBHOOK", "")

BASE = "https://open.feishu.cn/open-apis"
TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class FeishuEvent:
    fields: dict
    record_id: str = ""
    created_time: str = ""
    updated_time: str = ""


def get_token() -> str:
    r = requests.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"token 获取失败: {data}")
    return data["tenant_access_token"]


def _status_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            if key in value and value[key] is not None:
                return str(value[key]).strip().lower()
        return ""
    if isinstance(value, list):
        parts = [_status_text(item) for item in value]
        return ",".join(p for p in parts if p)
    return str(value).strip().lower()


def fetch_approved(token: str) -> list[FeishuEvent]:
    headers = {"Authorization": f"Bearer {token}"}
    events = []
    page_token = ""

    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token

        r = requests.get(
            f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records",
            headers=headers,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"读取表失败: {data}")

        for rec in data["data"].get("items", []):
            fields = rec.get("fields", {}) or {}
            if _status_text(fields.get("status")) != "approved":
                continue
            events.append(
                FeishuEvent(
                    fields=fields,
                    record_id=rec.get("record_id", ""),
                    created_time=rec.get("created_time", ""),
                    updated_time=rec.get("updated_time", ""),
                )
            )

        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token", "")

    return events


def parse_iso(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_date(value: str) -> dt.date | None:
    parsed = parse_iso(value)
    return parsed.astimezone(TZ).date() if parsed else None


def parse_updated_date(event: FeishuEvent) -> dt.date | None:
    updated = parse_iso(event.updated_time) or parse_iso(event.created_time)
    return updated.astimezone(TZ).date() if updated else None


def event_sort_key(event: FeishuEvent):
    start = parse_iso(event.fields.get("start_time", ""))
    city = event.fields.get("city") or ("线上" if event.fields.get("is_online") else "其他")
    name = event.fields.get("name_zh") or event.fields.get("name_en") or ""
    return (
        start or dt.datetime.max.replace(tzinfo=TZ),
        city,
        name,
    )


def group_by_city(events: list[FeishuEvent]) -> list[tuple[str, list[FeishuEvent]]]:
    grouped: dict[str, list[FeishuEvent]] = defaultdict(list)
    for event in events:
        city = event.fields.get("city") or ("线上" if event.fields.get("is_online") else "其他")
        grouped[city].append(event)

    return sorted(
        ((city, sorted(items, key=event_sort_key)) for city, items in grouped.items()),
        key=lambda item: item[0],
    )


def build_event_lines(event: FeishuEvent) -> list[str]:
    fields = event.fields
    name = fields.get("name_zh") or fields.get("name_en") or "（无标题）"
    venue = fields.get("venue") or ("线上" if fields.get("is_online") else "待定")
    url = fields.get("register_url") or fields.get("source_url") or ""
    tags = fields.get("topic_tags") or ""

    lines = [f"**{name}**"]
    lines.append(f"地点：{venue}")
    if url:
        lines.append(f"报名：{url}")
    if tags:
        lines.append(f"关键词：{tags}")
    return lines


def build_weekly_report(events: list[FeishuEvent], today: dt.date) -> list[dict]:
    monday = today - dt.timedelta(days=today.weekday())
    report_events = []

    for event in events:
        updated_day = parse_updated_date(event)
        if updated_day and monday <= updated_day <= today:
            report_events.append(event)

    if not report_events:
        return [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "本周暂无可回顾活动，敬请期待 🌱"},
            }
        ]

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"## 本周回顾（{monday.strftime('%m/%d')} - {today.strftime('%m/%d')}）"},
        }
    ]

    for city, city_events in group_by_city(report_events):
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"### {city}"},
            }
        )
        for event in city_events:
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(build_event_lines(event))},
                }
            )
            elements.append({"tag": "hr"})

        if elements and elements[-1].get("tag") == "hr":
            elements.pop()

    return elements


def build_card(events: list[FeishuEvent], today: dt.date, is_sunday: bool) -> dict:
    title = f"AI 活动播报 · {today.strftime('%m/%d')}"

    elements: list[dict] = []
    if not events:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "本期暂无已审核活动，敬请期待 🌱"},
            }
        )
    else:
        for event in sorted(events, key=event_sort_key):
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(build_event_lines(event))},
                }
            )
            elements.append({"tag": "hr"})

        if elements and elements[-1].get("tag") == "hr":
            elements.pop()

    if is_sunday:
        elements.append({"tag": "hr"})
        elements.extend(build_weekly_report(events, today))

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements
        + [
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"AI 活动雷达 · 数据来源 Luma / 活动行 / 联谱 / Devpost · {today.strftime('%Y-%m-%d')}",
                    }
                ],
            }
        ],
    }


def send(card: dict):
    if not BOT_WEBHOOK:
        raise RuntimeError("缺少 FEISHU_BOT_WEBHOOK")
    payload = {"msg_type": "interactive", "card": card}
    r = requests.post(BOT_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"推送失败: {data}")
    print("    [bot] 推送成功")


def main():
    today = dt.datetime.now(tz=TZ).date()
    is_sunday = today.weekday() == 6

    print(f"    [bot] 今天 {today} {'(周日周报)' if is_sunday else '(工作日播报)'}")

    token = get_token()
    all_events = fetch_approved(token)
    print(f"    [bot] 读取到 approved 活动 {len(all_events)} 条")

    card = build_card(all_events, today, is_sunday)
    send(card)


if __name__ == "__main__":
    main()
