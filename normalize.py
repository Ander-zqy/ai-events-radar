#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活动雷达 · normalize 层
============================================================
把各源 fetch_xxx() 产出的「生料」归一成数据契约结构。

输入:一个 dict(你现有 Event 的 asdict() 即可),至少含
      source / title / time / summary / location / url
      可选(以后 fetcher 补上,这里会自动用):
      organizer / start_at_raw / end_at_raw / topic_tags / lat / lng

输出:NormalizedEvent —— 16 字段契约结构,可直接喂给 translate / upsert。

设计原则:
  - 解析不出来的时间一律留空(""),不瞎猜;空的留给人在飞书里补,
    也方便加「无时间」哨兵。
  - 这一层只做「拆 + 归一」,翻译留给下一层(name_zh/name_en 先只填原文那侧)。
依赖:pip install dateparser
============================================================
"""

import re
import hashlib
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import dateparser


# ------------------------------------------------------------
# 契约结构
# ------------------------------------------------------------
@dataclass
class NormalizedEvent:
    id: str                       # = hash(source_url),天然去重
    source_url: str
    register_url: str
    source: str                   # luma / huodongxing / lianpu / devpost
    status: str = "pending"       # 审核闸门:pending / approved / rejected
    name_zh: str = ""
    name_en: str = ""
    start_time: str = ""          # ISO 8601 带时区,解析不出留空
    end_time: str = ""
    city: str = ""
    venue: str = ""
    is_online: bool = False
    desc_zh: str = ""
    desc_en: str = ""
    organizer: str = ""
    lang: str = ""                # 原文语种 zh / en
    topic_tags: str = ""          # 逗号分隔
    created_at: str = field(default_factory=lambda: _now_iso())
    updated_at: str = field(default_factory=lambda: _now_iso())


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------
# 源 → 默认时区(没有更精确信息时用)
# ------------------------------------------------------------
SOURCE_DEFAULT_TZ = {
    "huodongxing": "Asia/Shanghai",
    "lianpu": "Asia/Shanghai",
    "luma": "UTC",          # Luma 返回的是绝对 UTC 时刻
    "devpost": "UTC",
}

# 已知城市:别名 → (规范名, 时区)。可随时增删。
CITY_TABLE: Dict[str, Tuple[str, str]] = {
    "北京": ("北京", "Asia/Shanghai"), "beijing": ("北京", "Asia/Shanghai"),
    "上海": ("上海", "Asia/Shanghai"), "shanghai": ("上海", "Asia/Shanghai"),
    "深圳": ("深圳", "Asia/Shanghai"), "shenzhen": ("深圳", "Asia/Shanghai"),
    "杭州": ("杭州", "Asia/Shanghai"), "hangzhou": ("杭州", "Asia/Shanghai"),
    "广州": ("广州", "Asia/Shanghai"), "guangzhou": ("广州", "Asia/Shanghai"),
    "香港": ("香港", "Asia/Hong_Kong"), "hong kong": ("香港", "Asia/Hong_Kong"),
    "新加坡": ("新加坡", "Asia/Singapore"), "singapore": ("新加坡", "Asia/Singapore"),
    "旧金山": ("旧金山", "America/Los_Angeles"), "san francisco": ("旧金山", "America/Los_Angeles"),
    "洛杉矶": ("洛杉矶", "America/Los_Angeles"), "los angeles": ("洛杉矶", "America/Los_Angeles"),
    "纽约": ("纽约", "America/New_York"), "new york": ("纽约", "America/New_York"),
}

ONLINE_HINTS = ("线上", "online", "virtual", "remote", "webinar", "zoom", "腾讯会议", "直播")

# 轻量主题打标(从标题/简介关键词补 tag)
TAG_KEYWORDS = {
    "AI": ("ai", "人工智能", "gpt", "llm", "大模型"),
    "Agent": ("agent", "智能体"),
    "LLM": ("llm", "大模型", "language model"),
    "Hackathon": ("hackathon", "黑客松", "hack"),
    "出海": ("出海", "go global", "海外拓展", "全球化"),
    "创业": ("创业", "startup", "founder", "创投"),
}


# ------------------------------------------------------------
# 小工具
# ------------------------------------------------------------
_CJK = re.compile(r"[\u4e00-\u9fff]")
_TRACK_PARAMS = re.compile(r"^(utm_|fbclid|gclid|spm|from$)")


def detect_lang(*texts: str) -> str:
    """中文字符占比 > 15% 判为 zh,否则 en。"""
    s = " ".join(t for t in texts if t)
    if not s:
        return "en"
    cjk = len(_CJK.findall(s))
    letters = len(re.findall(r"[A-Za-z]", s))
    if cjk == 0:
        return "en"
    return "zh" if cjk * 4 >= letters else "en"


def canonical_url(u: str) -> str:
    """规范化 URL:小写 host、去 fragment、去跟踪参数、去末尾斜杠。用于稳定 id。"""
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        u = "https:" + u
    try:
        sp = urlsplit(u)
    except Exception:
        return u
    host = (sp.hostname or "").lower()
    if sp.port:
        host = f"{host}:{sp.port}"
    query = "&".join(
        kv for kv in sp.query.split("&")
        if kv and not _TRACK_PARAMS.match(kv.split("=")[0].lower())
    )
    path = sp.path.rstrip("/") or "/"
    return urlunsplit((sp.scheme or "https", host, path, query, ""))


def make_id(source_url: str) -> str:
    return hashlib.sha1(canonical_url(source_url).encode("utf-8")).hexdigest()[:16]


def keyword_tags(*texts: str) -> List[str]:
    blob = " ".join(t for t in texts if t).lower()
    return [tag for tag, kws in TAG_KEYWORDS.items() if any(k in blob for k in kws)]


# ------------------------------------------------------------
# 时间解析:各源格式不同,统一成带时区 ISO
# ------------------------------------------------------------
_RANGE_SEP = re.compile(r"\s*(?:-|–|—|~|至|到|to)\s*", re.IGNORECASE)
_HAS_YEAR = re.compile(r"\b\d{4}\b")
_ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]")
_ZH_MD = re.compile(r"(\d{1,2})月(\d{1,2})日")
_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def _parse_zh_md(text: str, tz: str) -> Optional[dt.datetime]:
    """中文「M月D日 [HH:MM]」无年份 → 推断为即将到来的那一年。"""
    m = _ZH_MD.search(text)
    if not m or "年" in text:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    tm = _HHMM.search(text)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    z = ZoneInfo(tz)
    today = dt.datetime.now(z).date()
    try:
        cand = dt.datetime(today.year, month, day, hh, mm, tzinfo=z)
    except ValueError:
        return None
    if cand.date() < today - dt.timedelta(days=2):   # 已过去 → 取明年那场
        cand = cand.replace(year=today.year + 1)
    return cand


def _parse_one(text: str, tz: str, base: dt.datetime) -> Optional[dt.datetime]:
    if not text or not text.strip():
        return None
    text = text.strip()
    if _ISO_LIKE.match(text) or text.endswith("Z"):
        d = dateparser.parse(text, settings={"RETURN_AS_TIMEZONE_AWARE": True})
        if d:
            return d
    zh = _parse_zh_md(text, tz)             # 中文月日快速通道
    if zh:
        return zh
    d = dateparser.parse(
        text,
        languages=["zh", "en"],
        settings={
            "TIMEZONE": tz,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": base,
        },
    )
    return d


def parse_time_range(raw_time: str, source: str,
                     start_raw: str = "", end_raw: str = "") -> Tuple[str, str]:
    """返回 (start_iso, end_iso),解析不出的部分留空。"""
    tz = SOURCE_DEFAULT_TZ.get(source, "UTC")
    base = dt.datetime.now()

    # 1) fetcher 已给结构化时间(如 Luma 的 start_at)→ 直接用,最准
    if start_raw or end_raw:
        s = _parse_one(start_raw, tz, base) if start_raw else None
        e = _parse_one(end_raw, tz, base) if end_raw else None
        return (s.isoformat() if s else "", e.isoformat() if e else "")

    txt = (raw_time or "").strip()
    if not txt or txt in ("待定", "TBD", "tbd"):
        return ("", "")

    parts = _RANGE_SEP.split(txt, maxsplit=1)
    if len(parts) == 2:
        left, right = parts[0].strip(), parts[1].strip()
        # 右边有年份、左边没有 → 把年份补给左边(如 "May 01 - Aug 15, 2025")
        if _HAS_YEAR.search(right) and not _HAS_YEAR.search(left):
            ym = _HAS_YEAR.search(right)
            left = f"{left} {ym.group(0)}"
        s = _parse_one(left, tz, base)
        e = _parse_one(right, tz, base)
        return (s.isoformat() if s else "", e.isoformat() if e else "")

    s = _parse_one(txt, tz, base)
    return (s.isoformat() if s else "", "")


# ------------------------------------------------------------
# 地点解析:拆成 is_online / city / venue
# ------------------------------------------------------------
def parse_location(raw_loc: str, city_hint: str = "") -> Tuple[bool, str, str]:
    loc = (raw_loc or "").strip()
    low = loc.lower()

    is_online = any(h in low or h in loc for h in ONLINE_HINTS)

    # 城市优先用 hint(如 Luma 源自带城市)
    city = ""
    if city_hint:
        canon = CITY_TABLE.get(city_hint.strip().lower()) or CITY_TABLE.get(city_hint.strip())
        city = canon[0] if canon else city_hint.strip()

    # 否则在文本里找已知城市
    if not city and loc:
        for alias, (canon, _tz) in CITY_TABLE.items():
            if alias in low or alias in loc:
                city = canon
                break

    # venue:去掉「该城市的所有别名」(含跨语言)与分隔符后的剩余
    venue = loc
    if city:
        aliases = [a for a, (canon, _tz) in CITY_TABLE.items() if canon == city]
        for a in sorted(aliases, key=len, reverse=True):
            venue = re.sub(re.escape(a), "", venue, flags=re.IGNORECASE)
    venue = re.sub(r"^[\s/／·,，|、-]+|[\s/／·,，|、-]+$", "", venue).strip()
    if is_online and (not venue or venue.lower() in ("online", "线上")):
        venue = ""

    if loc in ("待定", "") and not is_online:
        city = city or ""
    return (is_online, city, venue)


# ------------------------------------------------------------
# 主函数:一条生料 → 一条契约记录
# ------------------------------------------------------------
def normalize(raw: dict) -> NormalizedEvent:
    title = (raw.get("title") or "").strip()
    summary = (raw.get("summary") or "").strip()
    url = (raw.get("url") or "").strip()

    # 源字段里可能混了城市,如 "Luma·洛杉矶" → source=luma, city_hint=洛杉矶
    src_field = (raw.get("source") or "").strip()
    city_hint = ""
    if "·" in src_field:
        base_src, city_hint = src_field.split("·", 1)
    else:
        base_src = src_field
    source = base_src.strip().lower()
    source = {"luma": "luma", "活动行": "huodongxing", "联谱": "lianpu",
              "devpost": "devpost"}.get(base_src.strip(), source)

    lang = detect_lang(title, summary)
    start_iso, end_iso = parse_time_range(
        raw.get("time", ""), source,
        start_raw=raw.get("start_at_raw", ""),
        end_raw=raw.get("end_at_raw", ""),
    )
    is_online, city, venue = parse_location(raw.get("location", ""), city_hint)

    tags = set(keyword_tags(title, summary))
    extra = raw.get("topic_tags") or ""
    if extra:
        existing_lower = {t.lower() for t in tags}
        for t in re.split(r"[,，;；]", extra):
            t = t.strip()
            if t and t.lower() not in existing_lower:
                tags.add(t)
                existing_lower.add(t.lower())

    ne = NormalizedEvent(
        id=make_id(url),
        source_url=url,
        register_url=url,                       # 这些平台:列表链接即报名页
        source=source or "unknown",
        name_zh=title if lang == "zh" else "",  # 翻译层补另一侧
        name_en=title if lang == "en" else "",
        start_time=start_iso,
        end_time=end_iso,
        city=city,
        venue=venue,
        is_online=is_online,
        desc_zh=summary if lang == "zh" else "",
        desc_en=summary if lang == "en" else "",
        organizer=(raw.get("organizer") or "").strip(),
        lang=lang,
        topic_tags=", ".join(sorted(tags)),
    )
    return ne


# ============================================================
# 自测:四个源各一条真实样例,看解析质量
# ============================================================
if __name__ == "__main__":
    samples = [
        {  # Luma:你现有代码把时间砍成了日期;这里演示有 start_at_raw 时的最佳效果
            "source": "Luma·旧金山", "title": "AI Agents Summit 2026",
            "time": "2026-07-15", "summary": "",
            "location": "San Francisco", "url": "https://lu.ma/abc123?utm_source=x",
            "start_at_raw": "2026-07-15T01:00:00.000Z",
        },
        {  # 活动行:有年份的点分日期范围
            "source": "活动行", "title": "北京AI创业者大会",
            "time": "2026.06.21-2026.06.22", "summary": "",
            "location": "北京 中关村软件园", "url": "https://www.huodongxing.com/event/123456",
            "organizer": "极客邦科技",
        },
        {  # 联谱:中文、无年份、跨天
            "source": "联谱", "title": "Agent 黑客松",
            "time": "6月23日 00:00 - 6月24日 23:59",
            "summary": "两天一夜的 Agent 主题黑客松,组队开发 AI 应用",
            "location": "深圳 / 南山区", "url": "https://lianpu.com/event/agent-hack",
        },
        {  # Devpost:英文、年份在尾、线上
            "source": "Devpost", "title": "Global AI Hackathon",
            "time": "May 01 - Aug 15, 2025",
            "summary": "Machine Learning, Beginner Friendly",
            "location": "Online", "url": "https://global-ai.devpost.com",
        },
    ]

    for s in samples:
        ne = normalize(s)
        print("=" * 60)
        print(f"[{ne.source}] {ne.name_zh or ne.name_en}")
        print(f"  id        : {ne.id}")
        print(f"  lang      : {ne.lang}")
        print(f"  start_time: {ne.start_time or '(空)'}")
        print(f"  end_time  : {ne.end_time or '(空)'}")
        print(f"  is_online : {ne.is_online}")
        print(f"  city      : {ne.city or '(空)'}")
        print(f"  venue     : {ne.venue or '(空)'}")
        print(f"  organizer : {ne.organizer or '(空)'}")
        print(f"  tags      : {ne.topic_tags or '(空)'}")
        print(f"  reg_url   : {ne.register_url}")
