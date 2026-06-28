#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活动雷达 · enrich 层
============================================================
对「缺简介或缺主办方」的活动,进详情页把信息补回来。
跑在 normalize 之后、translate 之前 —— 让翻译层有原文可翻。

提取策略(从稳到脆):
  1. JSON-LD  <script type="application/ld+json"> @type=Event
     —— 金标准,自带 description / organizer / startDate / endDate / location
  2. og:description / meta[name=description]  —— 通用兜底
  3. 逐站选择器  —— 仅在前两者拿不到时按需补(留了 hook)

设计:
  - 只补缺的活动(有简介且有主办方就跳过,不进详情页)。
  - 按 id 持久缓存详情页提取结果,cron 重复跑不重复抓。
  - fetch 可注入:测试喂假 HTML,生产走真网络(带重试)。
  - 任意一页失败 → 留空待审,不中断整批。
依赖:requests, beautifulsoup4(你已装)
============================================================
"""

import re
import json
import time
import datetime as dt
from typing import Optional, Callable, List, Dict

import requests
from bs4 import BeautifulSoup

from normalize import NormalizedEvent, parse_location
from stability import safe_get

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}


# ------------------------------------------------------------
# 默认抓取器(走 stability.safe_get,自带重试/429/限速)—— 生产用
# ------------------------------------------------------------
def default_fetch(url: str, timeout: int = 20) -> Optional[str]:
    if not url:
        return None
    try:
        r = safe_get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"    [enrich] 抓取失败 {url}: {e}")
        return None


# ------------------------------------------------------------
# 通用提取:JSON-LD + og/meta
# ------------------------------------------------------------
def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        # 可能是单对象 / 数组 / @graph
        if isinstance(data, dict) and "@graph" in data:
            yield from (n for n in data["@graph"] if isinstance(n, dict))
        elif isinstance(data, list):
            yield from (n for n in data if isinstance(n, dict))
        elif isinstance(data, dict):
            yield data


def _is_event(node: dict) -> bool:
    t = node.get("@type", "")
    types = t if isinstance(t, list) else [t]
    return any("Event" in str(x) for x in types)


def _organizer_name(node: dict) -> str:
    org = node.get("organizer")
    if isinstance(org, dict):
        return (org.get("name") or "").strip()
    if isinstance(org, list) and org:
        first = org[0]
        return (first.get("name", "") if isinstance(first, dict) else str(first)).strip()
    if isinstance(org, str):
        return org.strip()
    return ""


def _location_from_jsonld(node: dict):
    """返回 (is_online, place_text)。"""
    loc = node.get("location")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        t = loc.get("@type", "")
        if "VirtualLocation" in str(t):
            return True, ""
        name = loc.get("name", "")
        addr = loc.get("address", "")
        if isinstance(addr, dict):
            addr = " ".join(str(addr.get(k, "")) for k in
                            ("addressLocality", "addressRegion", "streetAddress") if addr.get(k))
        text = " ".join(p for p in (str(name), str(addr)) if p).strip()
        return False, text
    if isinstance(loc, str):
        return False, loc.strip()
    return False, ""


def _meta(soup: BeautifulSoup, *queries) -> str:
    for attr, val in queries:
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def extract_generic(html: str, ev: NormalizedEvent) -> Dict:
    """返回提取到的字段子集:desc / organizer / start / end / is_online / place。"""
    out: Dict = {}
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")

    for node in _iter_jsonld(soup):
        if not _is_event(node):
            continue
        if node.get("description"):
            out["desc"] = re.sub(r"\s+", " ", str(node["description"])).strip()
        org = _organizer_name(node)
        if org:
            out["organizer"] = org
        if node.get("startDate"):
            out["start"] = str(node["startDate"])
        if node.get("endDate"):
            out["end"] = str(node["endDate"])
        online, place = _location_from_jsonld(node)
        out["is_online"] = online
        if place:
            out["place"] = place
        break   # 取第一个 Event 节点即可

    # 简介兜底:og:description / meta description
    if not out.get("desc"):
        desc = _meta(soup, ("property", "og:description"), ("name", "description"))
        if desc:
            out["desc"] = desc

    return out


# 逐站覆盖(默认全走通用;需要时在这里加站点专属解析)
SOURCE_EXTRACTORS: Dict[str, Callable] = {
    # "huodongxing": extract_huodongxing,   # 待看真实 HTML 后补专属选择器
    # "lianpu": extract_lianpu,
}


def extract(html: str, ev: NormalizedEvent) -> Dict:
    fn = SOURCE_EXTRACTORS.get(ev.source, extract_generic)
    return fn(html, ev)


# ------------------------------------------------------------
# 缓存(按 id)
# ------------------------------------------------------------
class DetailCache:
    def __init__(self, path: str = ".enrich_cache.json"):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self._d = json.load(f)
        except Exception:
            self._d = {}
        self.hits = 0
        self.fetched = 0

    def get(self, eid):
        v = self._d.get(eid)
        if v is not None:
            self.hits += 1
        return v

    def set(self, eid, val):
        self._d[eid] = val
        self.fetched += 1

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._d, f, ensure_ascii=False)


# ------------------------------------------------------------
# 应用提取结果到活动(只填空,不覆盖已有)
# ------------------------------------------------------------
def needs_enrich(ev: NormalizedEvent) -> bool:
    has_desc = bool(ev.desc_zh or ev.desc_en)
    return (not has_desc) or (not ev.organizer)


def _apply(ev: NormalizedEvent, ex: Dict):
    lang = ev.lang or "zh"
    desc_field = f"desc_{lang}"
    if ex.get("desc") and not getattr(ev, desc_field):
        setattr(ev, desc_field, ex["desc"])
    if ex.get("organizer") and not ev.organizer:
        ev.organizer = ex["organizer"]
    if ex.get("start") and not ev.start_time:
        ev.start_time = ex["start"]
    if ex.get("end") and not ev.end_time:
        ev.end_time = ex["end"]
    if ex.get("is_online"):
        ev.is_online = True
    if ex.get("place") and not ev.venue and not ev.city:
        online, city, venue = parse_location(ex["place"])
        ev.city = ev.city or city
        ev.venue = ev.venue or venue


def enrich_event(ev: NormalizedEvent, cache: DetailCache,
                 fetch: Callable[[str], Optional[str]]) -> bool:
    """返回是否真去抓了网络(用于决定是否礼貌等待)。"""
    if not needs_enrich(ev):
        return False
    ex = cache.get(ev.id)
    fetched = False
    if ex is None:
        html = fetch(ev.source_url)
        ex = extract(html, ev) if html else {}
        cache.set(ev.id, ex)
        fetched = True
    _apply(ev, ex)
    ev.updated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return fetched


def enrich(events: List[NormalizedEvent],
           cache_path: str = ".enrich_cache.json",
           fetch: Callable[[str], Optional[str]] = default_fetch,
           sleep: float = 1.0) -> List[NormalizedEvent]:
    cache = DetailCache(cache_path)
    todo = sum(1 for e in events if needs_enrich(e))
    for ev in events:
        fetched = enrich_event(ev, cache, fetch)
        if fetched and sleep:
            time.sleep(sleep)   # 只在真去抓时礼貌等待
    cache.save()
    print(f"    [enrich] 需补 {todo} 条 | 详情页缓存命中 {cache.hits} / 新抓 {cache.fetched}")
    return events


# ============================================================
# 自测:用构造的真实结构 HTML 验证解析(JSON-LD + og 兜底)
# ============================================================
if __name__ == "__main__":
    import os
    from normalize import normalize

    # 模拟两个详情页:Luma(英,JSON-LD 全)、联谱(中,JSON-LD)、活动行(只有 og 兜底)
    FAKE_PAGES = {
        "https://lu.ma/abc123": """
        <html><head>
        <script type="application/ld+json">
        {"@type":"Event","name":"AI Agents Summit 2026",
         "description":"A full-day summit on building production AI agents, with talks from leading labs.",
         "startDate":"2026-07-15T09:00:00-07:00","endDate":"2026-07-15T18:00:00-07:00",
         "organizer":{"@type":"Organization","name":"Agent Builders SF"},
         "location":{"@type":"Place","name":"Pier 27","address":{"addressLocality":"San Francisco"}}}
        </script></head><body></body></html>""",
        "https://www.huodongxing.com/event/123456": """
        <html><head>
        <meta property="og:description" content="面向 AI 创业者的两天大会,涵盖大模型落地、融资与出海实战。"/>
        </head><body></body></html>""",
    }

    def fake_fetch(url):
        return FAKE_PAGES.get(url)

    samples = [
        {"source": "Luma·旧金山", "title": "AI Agents Summit 2026", "time": "2026-07-15",
         "summary": "", "location": "San Francisco", "url": "https://lu.ma/abc123",
         "start_at_raw": "2026-07-15T01:00:00.000Z"},          # 缺简介+主办方 → 应补
        {"source": "活动行", "title": "北京AI创业者大会", "time": "2026.06.21-2026.06.22",
         "summary": "", "location": "北京 中关村", "url": "https://www.huodongxing.com/event/123456",
         "organizer": "极客邦科技"},                            # 缺简介 → 应补(og 兜底)
        {"source": "联谱", "title": "Agent 黑客松", "time": "6月23日",
         "summary": "两天一夜的 Agent 黑客松", "location": "深圳 / 南山区",
         "url": "https://lianpu.com/event/agent-hack", "organizer": "联谱社区"},  # 都齐 → 应跳过
    ]
    events = [normalize(s) for s in samples]

    cache_file = "/tmp/_enrich_demo.json"
    if os.path.exists(cache_file):
        os.remove(cache_file)

    enrich(events, cache_path=cache_file, fetch=fake_fetch, sleep=0)
    for ev in events:
        print("=" * 60)
        print(f"[{ev.source}] {ev.name_zh or ev.name_en}")
        print(f"  desc      : {(ev.desc_zh or ev.desc_en) or '(空)'}")
        print(f"  organizer : {ev.organizer or '(空)'}")
        print(f"  start_time: {ev.start_time or '(空)'}")
        print(f"  end_time  : {ev.end_time or '(空)'}")
        print(f"  city/venue: {ev.city or '(空)'} / {ev.venue or '(空)'}")
        print(f"  is_online : {ev.is_online}")

    print("\n第二次跑同一批(详情页应全部命中缓存,0 新抓):")
    events2 = [normalize(s) for s in samples]
    enrich(events2, cache_path=cache_file, fetch=fake_fetch, sleep=0)
