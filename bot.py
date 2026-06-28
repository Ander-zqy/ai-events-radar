#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活动雷达 · 飞书机器人推送 bot.py
============================================================
从飞书多维表格读取 approved 活动,按日期过滤后推送消息卡片。

规则:
  - 周一~周六:推「今日 + 未来3天」即将开始的活动
  - 周日:推「本周(周一~周日)」所有活动汇总
  - 没有活动:发一条「暂无活动」占位卡片(保持推送节奏)

触发方式:
  - GitHub Actions 每天定时跑(cron 配置见 scrape.yml)
  - 本地测试:python bot.py

环境变量:
  FEISHU_APP_ID / FEISHU_APP_SECRET   读多维表格用
  FEISHU_APP_TOKEN / FEISHU_TABLE_ID  多维表格坐标
  FEISHU_BOT_WEBHOOK                  群机器人 Webhook
============================================================
"""

import os
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import requests

load_dotenv()

# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
APP_ID       = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET   = os.environ.get("FEISHU_APP_SECRET", "")
APP_TOKEN    = os.environ.get("FEISHU_APP_TOKEN", "")
TABLE_ID     = os.environ.get("FEISHU_TABLE_ID", "")
BOT_WEBHOOK  = os.environ.get("FEISHU_BOT_WEBHOOK",
               "https://open.feishu.cn/open-apis/bot/v2/hook/1547dd46-c25c-40bf-a2ef-bc250ebba4b9")

BASE    = "https://open.feishu.cn/open-apis"
TZ      = ZoneInfo("Asia/Shanghai")


# ------------------------------------------------------------
# Token
# ------------------------------------------------------------
def get_token() -> str:
    r = requests.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"token 失败: {d}")
    return d["tenant_access_token"]


# ------------------------------------------------------------
# 读取多维表格 approved 活动
# ------------------------------------------------------------
def fetch_approved(token: str) -> list:
    headers = {"Authorization": f"Bearer {token}"}
    events, page_token = [], ""
    while True:
        params = {"page_size": 500, "filter": 'CurrentValue.[status]="approved"'}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(
            f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records",
            headers=headers, params=params, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"读表失败: {d}")
        for rec in d["data"].get("items", []):
            events.append(rec["fields"])
        if not d["data"].get("has_more"):
            break
        page_token = d["data"].get("page_token", "")
    return events


# ------------------------------------------------------------
# 日期过滤
# ------------------------------------------------------------
def ms_to_date(ms) -> datetime.date:
    """飞书日期字段 → date。"""
    if not ms:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000, tz=TZ).date()
    except Exception:
        return None


def filter_events(events: list, today: datetime.date, is_sunday: bool) -> list:
    result = []
    if is_sunday:
        # 本周一 ~ 本周日
        monday = today - datetime.timedelta(days=today.weekday())
        sunday = monday + datetime.timedelta(days=6)
        window = (monday, sunday)
    else:
        # 今天 ~ 今天+3天
        window = (today, today + datetime.timedelta(days=3))

    for ev in events:
        d = ms_to_date(ev.get("start_time"))
        if d and window[0] <= d <= window[1]:
            result.append(ev)

    # 按开始时间升序
    result.sort(key=lambda e: e.get("start_time") or 0)
    return result


# ------------------------------------------------------------
# 构建消息卡片
# ------------------------------------------------------------
def build_card(events: list, today: datetime.date, is_sunday: bool) -> dict:
    if is_sunday:
        title = f"📅 本周 AI 活动汇总（{today.strftime('%m/%d')} 周）"
    else:
        title = f"🔔 今日 AI 活动播报 · {today.strftime('%m月%d日')}"

    if not events:
        elements = [{"tag": "div", "text": {"tag": "lark_md",
                     "content": "**本期暂无已审核活动**，敬请期待 🌱"}}]
    else:
        elements = []
        for ev in events:
            name = ev.get("name_zh") or ev.get("name_en") or "（无标题）"
            start = ms_to_date(ev.get("start_time"))
            date_str = start.strftime("%m/%d") if start else "待定"
            city = ev.get("city") or ("线上" if ev.get("is_online") else "")
            organizer = ev.get("organizer") or ""
            tags = ev.get("topic_tags") or ""
            url = ev.get("register_url") or ""
            desc = ev.get("desc_zh") or ev.get("desc_en") or ""
            if len(desc) > 80:
                desc = desc[:80] + "..."

            # 每条活动一个区块
            lines = [f"**{name}**"]
            meta = " · ".join(p for p in [date_str, city, organizer] if p)
            if meta:
                lines.append(meta)
            if tags:
                tag_badges = " ".join(f"`{t.strip()}`" for t in tags.split(",") if t.strip())
                lines.append(tag_badges)
            if desc:
                lines.append(f"> {desc}")

            block = {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
            elements.append(block)

            if url:
                elements.append({
                    "tag": "action",
                    "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "📌 报名"},
                                 "type": "primary", "url": url}]
                })
            elements.append({"tag": "hr"})

        # 去掉最后多余的分割线
        if elements and elements[-1].get("tag") == "hr":
            elements.pop()

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue"
        },
        "elements": elements + [
            {"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": f"AI 活动雷达 · 数据来源 Luma / 活动行 / 联谱 / Devpost · {today.strftime('%Y-%m-%d')}"}
            ]}
        ]
    }
    return card


# ------------------------------------------------------------
# 推送
# ------------------------------------------------------------
def send(card: dict):
    payload = {"msg_type": "interactive", "card": card}
    r = requests.post(BOT_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"推送失败: {d}")
    print(f"    [bot] 推送成功")


# ------------------------------------------------------------
# 主入口
# ------------------------------------------------------------
def main():
    today = datetime.datetime.now(tz=TZ).date()
    is_sunday = today.weekday() == 6   # 0=周一 … 6=周日

    print(f"    [bot] 今天 {today} {'(周日·本周汇总)' if is_sunday else '(工作日·今日播报)'}")

    token = get_token()
    all_events = fetch_approved(token)
    print(f"    [bot] 读到 approved 活动 {len(all_events)} 条")

    filtered = filter_events(all_events, today, is_sunday)
    print(f"    [bot] 过滤后 {len(filtered)} 条")

    card = build_card(filtered, today, is_sunday)
    send(card)


if __name__ == "__main__":
    main()
