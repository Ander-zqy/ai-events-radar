#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 活动雷达 - 飞书机器人推送

规则:
  - 平日: 推送所有 approved 活动
  - 周日: 先推送所有 approved 活动，再附加一段周报汇总
  - 周报: 按城市分组，同一城市内按时间排序

周报展示字段:
  - 活动名称
  - 地点
  - 报名链接
  - 主题关键词

说明:
  - 周报按 approved_at 回顾
  - 如果 approved_at 缺失，bot 会在运行时自动补写当前时间
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
BATCH = 500


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
    events: list[FeishuEvent] = []
    page_token = ""
    sample_rows = []

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

            if len(sample_rows) < 5:
                sample_rows.append(
                    {
                        "record_id": rec.get("record_id", ""),
                        "status_raw": fields.get("status"),
                        "field_names": sorted(fields.keys()),
                    }
                )

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

    if not events:
        print(f"    [bot] 调试: 首批记录样本 {sample_rows}")

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


def parse_feishu_datetime(value) -> dt.datetime | None:
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value) / 1000, tz=TZ)
        except Exception:
            return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None

        if text.isdigit():
            try:
                return dt.datetime.fromtimestamp(int(text) / 1000, tz=TZ)
            except Exception:
                return None

        # 兼容飞书里常见的日期格式：2026/7/5、2026-07-05、2026/7/5 09:30
        for sep in ("/", "-"):
            if sep in text:
                parts = text.replace("T", " ").split(" ", 1)
                date_part = parts[0]
                time_part = parts[1].strip() if len(parts) > 1 else ""
                date_bits = date_part.split(sep)

                if len(date_bits) == 3 and all(bit.isdigit() for bit in date_bits):
                    year, month, day = map(int, date_bits)
                    hour = minute = second = 0

                    if time_part:
                        time_bits = time_part.split(":")
                        if len(time_bits) >= 2 and all(bit.strip().isdigit() for bit in time_bits[:2]):
                            hour = int(time_bits[0])
                            minute = int(time_bits[1])
                            if len(time_bits) >= 3 and time_bits[2].strip().isdigit():
                                second = int(time_bits[2])

                    try:
                        return dt.datetime(year, month, day, hour, minute, second, tzinfo=TZ)
                    except Exception:
                        pass

        return parse_iso(text)

    return parse_iso(str(value))


def format_feishu_datetime(value) -> str:
    parsed = parse_feishu_datetime(value)
    if not parsed:
        return ""
    return parsed.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def parse_approved_date(event: FeishuEvent) -> dt.date | None:
    approved = parse_feishu_datetime(event.fields.get("approved_at"))
    if approved:
        return approved.astimezone(TZ).date()
    return None


def _first_text(*values) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def sync_missing_approved_at(token: str, events: list[FeishuEvent]) -> int:
    now_ms = int(dt.datetime.now(tz=TZ).timestamp() * 1000)
    updates = []

    for event in events:
        if event.fields.get("approved_at"):
            continue
        updates.append(
            {
                "record_id": event.record_id,
                "fields": {"approved_at": now_ms},
            }
        )
        event.fields["approved_at"] = now_ms

    if not updates:
        return 0

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for i in range(0, len(updates), BATCH):
        chunk = updates[i : i + BATCH]
        r = requests.post(
            f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/batch_update",
            headers=headers,
            json={"records": chunk},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"自动补写 approved_at 失败: {data}")

    print(f"    [bot] 自动补写 approved_at {len(updates)} 条")
    return len(updates)


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


def build_daily_lines(event: FeishuEvent) -> list[str]:
    fields = event.fields
    name = _first_text(fields.get("name_zh"), fields.get("name_en"), "（无标题）")
    organizer = _first_text(fields.get("organizer"))
    co_organizer = _first_text(
        fields.get("co_organizer"),
        fields.get("co_organizers"),
        fields.get("co_host"),
        fields.get("co_hosts"),
    )
    city = _first_text(fields.get("city"), "线上" if fields.get("is_online") else "")
    venue = _first_text(fields.get("venue"))
    url = _first_text(fields.get("register_url"), fields.get("source_url"))
    tags = _first_text(fields.get("topic_tags"))
    approved_at = format_feishu_datetime(fields.get("approved_at"))

    lines = [f"**{name}**"]
    if organizer:
        lines.append(f"主办方：{organizer}")
    if co_organizer:
        lines.append(f"联办方：{co_organizer}")
    if city:
        lines.append(f"城市：{city}")
    if venue:
        lines.append(f"地点：{venue}")
    if url:
        lines.append(f"报名：{url}")
    if tags:
        lines.append(f"关键词：{tags}")
    if approved_at:
        lines.append(f"审核时间：{approved_at}")
    return lines


def build_weekly_lines(event: FeishuEvent) -> list[str]:
    fields = event.fields
    name = _first_text(fields.get("name_zh"), fields.get("name_en"), "（无标题）")
    venue = _first_text(fields.get("venue"), "线上" if fields.get("is_online") else "待定")
    url = _first_text(fields.get("register_url"), fields.get("source_url"))
    tags = _first_text(fields.get("topic_tags"))

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
        approved_day = parse_approved_date(event)
        if approved_day and monday <= approved_day <= today:
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
                    "text": {"tag": "lark_md", "content": "\n".join(build_weekly_lines(event))},
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
                    "text": {"tag": "lark_md", "content": "\n".join(build_daily_lines(event))},
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

    # 如果 approved_at 为空，先自动补写，方便周报按 approved_at 回顾
    sync_missing_approved_at(token, all_events)

    card = build_card(all_events, today, is_sunday)
    send(card)


if __name__ == "__main__":
    main()
