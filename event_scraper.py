#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活动雷达 · 爬虫 v2
============================================================
抓取创新活动(AI / 黑客松 等),输出 CSV + JSON,供人工筛选。

已实现的源:
    - Devpost   全球最大黑客松平台(公开 JSON 接口,无需鉴权)
    - 活动行     国内活动平台(列表页服务端渲染,可直接解析)
    - Luma       发现接口 get-paginated-events(按城市坐标 + 分类翻页)

输出列(按你的要求):活动名称 | 时间 | 活动简介 | 地点 | 链接

用法:
    pip install requests beautifulsoup4
    python event_scraper.py

输出(运行目录):
    events_YYYY-MM-DD.csv     用飞书 / Excel 打开,逐行筛选
    events_YYYY-MM-DD.json
============================================================
"""

import csv
import json
import re
import time
import html
import datetime
from dataclasses import dataclass, asdict
from typing import List

import requests
from bs4 import BeautifulSoup

from stability import safe_get

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
JSON_HEADERS = {"User-Agent": UA, "Accept": "application/json"}
HTML_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
TIMEOUT = 20


# ------------------------------------------------------------
# 统一活动结构 —— 所有源归一到这五列
# ------------------------------------------------------------
@dataclass
class Event:
    source: str            # 内部记录来源(不写进 CSV)
    title: str             # 活动名称
    time: str = "待定"      # 时间
    summary: str = ""      # 活动简介
    location: str = "待定"  # 地点
    url: str = ""          # 链接
    # —— 透传给 normalize 的额外信号(列表页能拿就拿,拿不到留空)——
    organizer: str = ""        # 主办方
    start_at_raw: str = ""     # 原始 ISO 开始时间(优先于 time 解析)
    end_at_raw: str = ""       # 原始 ISO 结束时间
    topic_tags: str = ""       # 源自带的主题标签

    def key(self):
        return (self.title.strip().lower(), (self.url or "").strip().lower())


def strip_html(s: str) -> str:
    if not s:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# ============================================================
# 源 1:Devpost(公开 JSON 接口,无需鉴权)
# ============================================================
DEVPOST_API = "https://devpost.com/api/hackathons"


def fetch_devpost(max_pages: int = 3) -> List[Event]:
    out: List[Event] = []
    combos = [{"challenge_type[]": "online"}, {"challenge_type[]": "in-person"}]
    for combo in combos:
        for status in ("upcoming", "open"):
            for page in range(1, max_pages + 1):
                params = {"status[]": status, "order_by": "deadline", "page": page}
                params.update(combo)
                try:
                    r = safe_get(DEVPOST_API, params=params,
                                     headers=JSON_HEADERS, timeout=TIMEOUT)
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    print(f"    [Devpost] {combo} {status} p{page} 失败: {e}")
                    break
                hacks = data.get("hackathons", [])
                if not hacks:
                    break
                for h in hacks:
                    dl = h.get("displayed_location") or {}
                    loc = dl.get("location", "") if isinstance(dl, dict) else ""
                    themes = ", ".join(
                        t.get("name", "") for t in (h.get("themes") or []) if isinstance(t, dict)
                    )
                    organizer = (h.get("organization_name", "") or "").strip()
                    url = h.get("url", "") or ""
                    if url.startswith("//"):
                        url = "https:" + url
                    title = (h.get("title", "") or "").strip()
                    if not title:
                        continue
                    out.append(Event(
                        source="Devpost",
                        title=title,
                        time=(h.get("submission_period_dates") or "待定").strip() or "待定",
                        summary="",                 # 真简介交给 enrich 抓详情页
                        location=loc or "Online",
                        url=url,
                        organizer=organizer,
                        topic_tags=themes,          # 主题标签进 topic_tags,不再混进简介
                    ))
                time.sleep(1)
    return out


# ============================================================
# 源 2:活动行(列表页服务端渲染,直接解析 HTML)
# 列表页:https://www.huodongxing.com/eventlist?tag=AI
#         https://www.huodongxing.com/eventlist?city=北京
# 说明:列表页能拿到 名称/时间/地点/链接;简介在详情页(单页应用),
#       不在这里抓,留给第二步由链接生成。
# ============================================================
HDX_BASE = "https://www.huodongxing.com"
EVENT_HREF_RE = re.compile(r"/event/(\d+)")
DATE_RE = re.compile(r"\d{4}\.\d{2}\.\d{2}(?:-\d{4}\.\d{2}\.\d{2})?")


def fetch_huodongxing(tags=None, cities=None) -> List[Event]:
    tags = tags or ["AI", "创业", "科技"]
    cities = cities or []
    list_urls = [f"{HDX_BASE}/eventlist?tag={t}" for t in tags]
    list_urls += [f"{HDX_BASE}/eventlist?city={c}" for c in cities]

    out: List[Event] = []
    seen = set()
    for url in list_urls:
        try:
            r = safe_get(url, headers=HTML_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            print(f"    [活动行] 请求失败 {url}: {e}")
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        # 以稳定的 /event/{id} 链接为锚点,逐个还原卡片
        for a in soup.find_all("a", href=EVENT_HREF_RE):
            m = EVENT_HREF_RE.search(a.get("href", ""))
            if not m:
                continue
            eid = m.group(1)
            if eid in seen:
                continue

            img = a.find("img")
            title = (img.get("alt", "").strip()
                     if img and img.get("alt") else a.get_text(strip=True))
            if not title or title in ("立即报名", "立即"):
                continue

            # 向上找「含日期」的祖先当作卡片,提取时间与地点
            card, time_txt, loc_txt, organizer_txt = a, "", "", ""
            for _ in range(6):
                if card is None:
                    break
                text = card.get_text(" ", strip=True)
                md = DATE_RE.search(text)
                if md:
                    time_txt = md.group(0)
                    after = text[md.end():].split("立即报名")[0]
                    org = card.find("a", href=re.compile(r"/org/\d+"))
                    if org:
                        ot = org.get_text(strip=True)
                        if ot:
                            organizer_txt = ot          # 接住主办方,别再丢了
                            after = after.replace(ot, " ")
                    loc_txt = re.sub(r"\s+", " ", after).strip(" ·|，,")
                    break
                card = card.parent

            seen.add(eid)
            out.append(Event(
                source="活动行",
                title=title,
                time=time_txt or "待定",
                summary="",   # 详情页是单页,简介留给 enrich
                location=(loc_txt[:60] if loc_txt else "待定"),
                url=f"{HDX_BASE}/event/{eid}",
                organizer=organizer_txt,
            ))
        time.sleep(1)
    return out


# ============================================================
# 源 3:Luma(发现接口 get-paginated-events,已接入)
# 接口:GET https://api.luma.com/discover/get-paginated-events
#   参数:latitude / longitude(城市坐标)、slug(分类,如 ai)、
#         pagination_limit(每页条数)、pagination_cursor(翻页游标)
#   返回:{ entries: [...], has_more: bool, next_cursor: str }
#   每个 entry:{ api_id, start_at, event: { name, url/slug, geo_address_info ... } }
# ============================================================
LUMA_API = "https://api.luma.com/discover/get-paginated-events"

# 内置常用城市坐标(以后按需增删)
LUMA_CITIES = {
    "洛杉矶": (34.05223, -118.24368),
    "旧金山": (37.77493, -122.41942),
    "纽约": (40.71278, -74.00597),
    "北京": (39.90420, 116.40739),
    "上海": (31.23039, 121.47370),
    "新加坡": (1.35208, 103.81984),
    "香港": (22.31930, 114.16936),
}


def _luma_pick(d: dict, *keys):
    """从 dict 里按优先级取第一个非空字段"""
    for k in keys:
        if isinstance(d, dict) and d.get(k):
            return d[k]
    return ""


def _luma_location(ev: dict) -> str:
    # 地点可能在 geo_address_info 里,也可能只标了线上
    geo = ev.get("geo_address_info") or {}
    if isinstance(geo, dict):
        loc = _luma_pick(geo, "full_address", "address", "city_state", "city", "name")
        if loc:
            return str(loc)
    if ev.get("location_type") == "online" or ev.get("is_online"):
        return "线上"
    return "待定"


def _luma_url(ev: dict, entry: dict) -> str:
    u = _luma_pick(ev, "url")
    if u:
        return u if str(u).startswith("http") else f"https://lu.ma/{u}"
    slug = _luma_pick(ev, "slug")
    if slug:
        return f"https://lu.ma/{slug}"
    api_id = entry.get("api_id") or ev.get("api_id") or ""
    return f"https://lu.ma/{api_id}" if api_id else ""


def fetch_luma(cities=None, slug="ai", max_pages=2, per_page=25) -> List[Event]:
    cities = cities or ["洛杉矶", "旧金山", "纽约", "北京", "上海"]
    out: List[Event] = []
    for city in cities:
        if city not in LUMA_CITIES:
            print(f"    [Luma] 未知城市 {city},跳过")
            continue
        lat, lng = LUMA_CITIES[city]
        cursor = None
        for _ in range(max_pages):
            params = {
                "latitude": lat,
                "longitude": lng,
                "pagination_limit": per_page,
            }
            if slug:
                params["slug"] = slug
            if cursor:
                params["pagination_cursor"] = cursor
            try:
                r = safe_get(LUMA_API, params=params, headers=JSON_HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"    [Luma] {city} 请求失败: {e}")
                break

            entries = data.get("entries", []) or []
            for entry in entries:
                ev = entry.get("event") or {}
                if not isinstance(ev, dict):
                    continue
                title = _luma_pick(ev, "name")
                if not title:
                    continue
                start_raw = entry.get("start_at") or _luma_pick(ev, "start_at") or ""
                end_raw = entry.get("end_at") or _luma_pick(ev, "end_at") or ""
                # time 字段仍留个好看的日期(向后兼容);完整 ISO 走 start_at_raw
                start_disp = str(start_raw)[:10] if "T" in str(start_raw) else (start_raw or "待定")
                # 主办方:Luma 常放在 calendar.name 或 hosts[0].name
                cal = ev.get("calendar") if isinstance(ev.get("calendar"), dict) else {}
                hosts = ev.get("hosts") if isinstance(ev.get("hosts"), list) else []
                organizer = ""
                if cal.get("name"):
                    organizer = str(cal["name"]).strip()
                elif hosts and isinstance(hosts[0], dict):
                    organizer = str(hosts[0].get("name", "")).strip()
                out.append(Event(
                    source=f"Luma·{city}",
                    title=str(title).strip(),
                    time=start_disp,
                    summary="",  # 简介在详情,留给 enrich
                    location=_luma_location(ev),
                    url=_luma_url(ev, entry),
                    organizer=organizer,
                    start_at_raw=str(start_raw),
                    end_at_raw=str(end_raw),
                    topic_tags=(slug or ""),
                ))

            if not data.get("has_more") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
            time.sleep(1)
        time.sleep(1)
    return out


# ============================================================
# 源 4:联谱 Lianpu(服务端渲染,列表页直接含简介)
# 标签页:https://lianpu.com/tag/ai   /tag/hackathon  /tag/agent ...
# 城市页:https://lianpu.com/city/beijing  /city/shenzhen ...
# 每个活动卡片含:链接(/event/{slug})、名称、简介、日期、城市/地点、主办方
# ============================================================
LIANPU_BASE = "https://lianpu.com"
LIANPU_EVENT_RE = re.compile(r"^/event/[\w\-]+$")
# 日期形如 "6月21日 09:00 - 18:00" 或 "6月23日 00:00 - 6月24日 23:59"
LIANPU_DATE_RE = re.compile(r"\d{1,2}月\d{1,2}日[\s\d:：\-~至到]*")


def fetch_lianpu(tags=None, cities=None) -> List[Event]:
    tags = tags or ["ai", "hackathon", "agent", "vibe-coding", "opc"]
    cities = cities or []
    list_urls = [f"{LIANPU_BASE}/tag/{t}" for t in tags]
    list_urls += [f"{LIANPU_BASE}/city/{c}" for c in cities]

    out: List[Event] = []
    seen = set()
    for url in list_urls:
        try:
            r = safe_get(url, headers=HTML_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            print(f"    [联谱] 请求失败 {url}: {e}")
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        # 活动标题锚点:<a href="/event/{slug}"> 且 a 在标题(h3)里
        # 用所有指向 /event/ 的链接,取其所在卡片
        for a in soup.find_all("a", href=LIANPU_EVENT_RE):
            slug = a.get("href", "").split("/event/")[-1]
            if not slug or slug in seen:
                continue
            title = a.get_text(strip=True)
            # 标题链接通常文字就是活动名;跳过空文字(图片链接)
            if not title:
                continue

            # 卡片容器:向上找一个同时含「日期」的祖先
            card, date_txt, loc_txt, summary = a, "", "", ""
            node = a
            for _ in range(6):
                node = node.parent
                if node is None:
                    break
                text = node.get_text(" ", strip=True)
                if LIANPU_DATE_RE.search(text):
                    card = node
                    break

            if card is not None:
                ctext = card.get_text("\n", strip=True)
                md = LIANPU_DATE_RE.search(card.get_text(" ", strip=True))
                if md:
                    date_txt = md.group(0).strip()
                # 地点:含「/」或城市名的那一行(报名后获取... 也算)
                lines = [ln.strip() for ln in ctext.split("\n") if ln.strip()]
                for ln in lines:
                    if (" / " in ln or "／" in ln) and not ln.startswith("http"):
                        loc_txt = ln
                        break
                # 简介:最长的一行文字(排除标题、日期、价格、地点)
                cand = [ln for ln in lines
                        if ln != title
                        and not LIANPU_DATE_RE.search(ln)
                        and ln != loc_txt
                        and not ln.startswith("￥")
                        and ln not in ("免费", "投稿")
                        and len(ln) >= 10]
                if cand:
                    summary = max(cand, key=len)

            seen.add(slug)
            out.append(Event(
                source="联谱",
                title=title,
                time=date_txt or "待定",
                summary=summary,
                location=loc_txt or "待定",
                url=f"{LIANPU_BASE}/event/{slug}",
            ))
        time.sleep(1)
    return out


# 启用的源
SOURCES = [
    ("Devpost", fetch_devpost),
    ("活动行", fetch_huodongxing),
    ("Luma", fetch_luma),
    ("联谱", fetch_lianpu),
]


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 50)
    print("活动雷达 · 开始抓取")
    print("=" * 50)

    all_events: List[Event] = []
    for name, fn in SOURCES:
        print(f"\n→ {name} ...")
        try:
            evs = fn()
            print(f"    得到 {len(evs)} 条")
            all_events.extend(evs)
        except Exception as e:
            print(f"    [{name}] 整体失败,已跳过: {e}")

    seen, deduped = set(), []
    for e in all_events:
        if e.key() in seen:
            continue
        seen.add(e.key())
        deduped.append(e)
    print(f"\n去重后共 {len(deduped)} 条活动")

    today = datetime.date.today().isoformat()
    csv_path = f"events_{today}.csv"
    json_path = f"events_{today}.json"

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["活动名称", "时间", "活动简介", "地点", "链接"])
        for e in deduped:
            w.writerow([e.title, e.time, e.summary, e.location, e.url])

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in deduped], f, ensure_ascii=False, indent=2)

    print(f"\n已写出:\n  {csv_path}\n  {json_path}")
    print("\n下一步:打开 CSV 逐行筛,把选中活动的『链接』发我,")
    print("我把每个活动整理成飞书可复制的格式(含简介)。")


if __name__ == "__main__":
    main()


# ============================================================
# 如何新增更多源(微信 RSS / 小红书话题 / 其它站)
# ------------------------------------------------------------
# 跟我们接 Luma 的流程一模一样:
#   1. Chrome 打开目标站的「列表页」,F12 → Network → 勾 Fetch/XHR
#   2. 用搜索框输入页面上某个真实活动名,定位到「返回一批活动」的请求
#   3. 右键 → 复制 → 复制为 cURL,连同一个展开的条目字段发我
#   4. 我照着 fetch_luma() 的写法,给你接成一个新的 fetch_xxx()
#
# Luma 城市坐标:在 LUMA_CITIES 里加一行即可,格式 "城市": (纬度, 经度)
# Luma 分类:把 fetch_luma(slug="ai") 的 slug 换成别的分类即可
# ============================================================