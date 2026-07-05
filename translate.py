#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活动雷达 · translate 层
============================================================
给 normalize 后的活动补上「缺失的那门语言」(中→补英 / 英→补中)。

设计:
  - 只翻 name 和 desc(主办方/场馆是专有名词,留原文)。
  - 只补空的那侧,已有原文不动。
  - 持久缓存:按 (引擎, 源, 目标, 原文) 缓存,每天 cron 重复跑不重复翻。
  - 单条活动把 name+desc 合并成一次请求(省调用),按字段写回缓存。
  - 引擎可插拔:
        OpenAITranslator    —— 默认,LLM 翻译,带 AI 活动术语表
        EchoTranslator      —— 离线桩,仅供测试编排逻辑
        (GoogleTranslator   —— 可选免费引擎,见文末注释)
  - 任意一条翻译失败 → 该字段留空,不中断整批,交给审核员补。

用法:
    from normalize import normalize
    from translate import translate, OpenAITranslator
    events = [normalize(d) for d in raw_dicts]
    events = translate(events, engine=OpenAITranslator())   # 需 OPENAI_API_KEY
============================================================
"""

import os
import json
import time
import hashlib
import datetime as dt
from dataclasses import asdict
from typing import List, Protocol

import requests

from normalize import NormalizedEvent


# ------------------------------------------------------------
# AI 活动术语表(注入 LLM,保证术语统一)
# ------------------------------------------------------------
GLOSSARY = {
    "大模型": "large language model (LLM)",
    "黑客松": "hackathon",
    "智能体": "AI agent",
    "路演": "demo day",
    "出海": "going global",
    "创投": "venture & startup",
    "闭门": "closed-door",
    "分享会": "talk",
    "沙龙": "salon / meetup",
    "具身智能": "embodied AI",
}


# ------------------------------------------------------------
# 引擎协议
# ------------------------------------------------------------
class Translator(Protocol):
    id: str
    def translate_batch(self, texts: List[str], src: str, tgt: str) -> List[str]:
        """输入一批原文,返回等长的译文列表(顺序一一对应)。"""
        ...


# ------------------------------------------------------------
# 引擎 1:OpenAI LLM(默认,推荐)
# ------------------------------------------------------------
class OpenAITranslator:
    id = "openai"

    def __init__(self, model: str = None,
                 api_key: str = None, timeout: int = 60):
        # mini 兼顾翻译质量、速度与成本；可用 OPENAI_MODEL 覆盖。
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout

    def translate_batch(self, texts: List[str], src: str, tgt: str) -> List[str]:
        if not texts:
            return []
        if not self.api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY")

        lang_name = {"zh": "Simplified Chinese", "en": "English"}
        gloss = "\n".join(f"  {k} → {v}" for k, v in GLOSSARY.items())
        instructions = (
            f"You translate AI/tech event copy from {lang_name.get(src, src)} "
            f"to {lang_name.get(tgt, tgt)}. Keep it natural and concise; preserve "
            f"product/brand names and acronyms as-is. Use this glossary:\n{gloss}\n"
            "Return translated strings in exactly the same length and order as "
            "the input array. Do not add commentary."
        )
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": json.dumps(texts, ensure_ascii=False),
            "max_output_tokens": 4096,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "event_translations",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "translations": {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                        },
                        "required": ["translations"],
                        "additionalProperties": False,
                    },
                }
            },
        }
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "authorization": f"Bearer {self.api_key.strip()}",
                "content-type": "application/json",
            },
            json=payload, timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(
            part.get("text", "")
            for item in data.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ).strip()
        if not text:
            raise ValueError(f"OpenAI 未返回文本: status={data.get('status')!r}")
        parsed = json.loads(text)
        out = parsed.get("translations")
        if not isinstance(out, list) or len(out) != len(texts):
            raise ValueError(f"译文条数不匹配:期望 {len(texts)} 得到 {out!r}")
        return [str(x) for x in out]


# ------------------------------------------------------------
# 引擎 2:离线桩(仅测试编排;不联网)
# ------------------------------------------------------------
class EchoTranslator:
    id = "echo"
    # 给 demo 用的极小词典,让输出看起来像样;真翻译请用 OpenAITranslator
    _MINI = {
        "北京AI创业者大会": "Beijing AI Founders Conference",
        "Agent 黑客松": "Agent Hackathon",
        "两天一夜的 Agent 主题黑客松,组队开发 AI 应用":
            "A two-day Agent-themed hackathon; team up to build AI apps",
    }

    def translate_batch(self, texts: List[str], src: str, tgt: str) -> List[str]:
        return [self._MINI.get(t, f"[{tgt}] {t}") for t in texts]


# ------------------------------------------------------------
# 持久缓存
# ------------------------------------------------------------
class TranslationCache:
    def __init__(self, path: str = ".translation_cache.json"):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self._d = json.load(f)
        except Exception:
            self._d = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _k(engine: str, src: str, tgt: str, text: str) -> str:
        return hashlib.sha1(f"{engine}|{src}|{tgt}|{text}".encode("utf-8")).hexdigest()

    def get(self, engine, src, tgt, text):
        v = self._d.get(self._k(engine, src, tgt, text))
        if v is not None:
            self.hits += 1
        return v

    def set(self, engine, src, tgt, text, val):
        self._d[self._k(engine, src, tgt, text)] = val
        self.misses += 1

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._d, f, ensure_ascii=False)


# ------------------------------------------------------------
# 编排:一条活动 → 补全双语
# ------------------------------------------------------------
def _targets(ev: NormalizedEvent):
    """返回 (源语言, 目标语言)。"""
    src = ev.lang or "zh"
    tgt = "en" if src == "zh" else "zh"
    return src, tgt


def translate_event(ev: NormalizedEvent, engine: Translator, cache: TranslationCache) -> NormalizedEvent:
    src, tgt = _targets(ev)
    name_src = ev.name_zh if src == "zh" else ev.name_en
    desc_src = ev.desc_zh if src == "zh" else ev.desc_en

    # 收集需要翻的字段(目标侧为空、且源侧有内容、且缓存没有)
    jobs = []          # (字段名, 原文)
    for fieldname, text in (("name", name_src), ("desc", desc_src)):
        if not text:
            continue
        tgt_field = f"{fieldname}_{tgt}"
        if getattr(ev, tgt_field):          # 目标侧已有(极少)→ 跳过
            continue
        cached = cache.get(engine.id, src, tgt, text)
        if cached is not None:
            setattr(ev, tgt_field, cached)
        else:
            jobs.append((fieldname, text))

    if jobs:
        try:
            results = engine.translate_batch([t for _, t in jobs], src, tgt)
            for (fieldname, text), translated in zip(jobs, results):
                setattr(ev, f"{fieldname}_{tgt}", translated)
                cache.set(engine.id, src, tgt, text, translated)
        except Exception as e:
            print(f"    [translate] 失败,留空待审: {ev.id} {e}")

    ev.updated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return ev


def translate(events: List[NormalizedEvent], engine: Translator = None,
              cache_path: str = ".translation_cache.json",
              sleep: float = 0.0) -> List[NormalizedEvent]:
    engine = engine or OpenAITranslator()
    cache = TranslationCache(cache_path)
    for ev in events:
        translate_event(ev, engine, cache)
        if sleep:
            time.sleep(sleep)
    cache.save()
    print(f"    [translate] 缓存命中 {cache.hits} / 新翻译 {cache.misses}")
    return events


# ============================================================
# 自测:用离线桩验证编排(只补缺、缓存、失败留空)
# ============================================================
if __name__ == "__main__":
    from normalize import normalize

    samples = [
        {"source": "活动行", "title": "北京AI创业者大会", "time": "2026.06.21-2026.06.22",
         "summary": "", "location": "北京 中关村软件园",
         "url": "https://www.huodongxing.com/event/123456", "organizer": "极客邦科技"},
        {"source": "联谱", "title": "Agent 黑客松", "time": "6月23日 00:00 - 6月24日 23:59",
         "summary": "两天一夜的 Agent 主题黑客松,组队开发 AI 应用",
         "location": "深圳 / 南山区", "url": "https://lianpu.com/event/agent-hack"},
        {"source": "Luma·旧金山", "title": "AI Agents Summit 2026", "time": "2026-07-15",
         "summary": "", "location": "San Francisco", "url": "https://lu.ma/abc123",
         "start_at_raw": "2026-07-15T01:00:00.000Z"},
    ]
    events = [normalize(s) for s in samples]

    cache_file = "/tmp/_tcache_demo.json"
    if os.path.exists(cache_file):
        os.remove(cache_file)

    print("第一次跑(全新翻译):")
    events = translate(events, engine=EchoTranslator(), cache_path=cache_file)
    for ev in events:
        print("=" * 60)
        print(f"  zh: {ev.name_zh or '(空)'}")
        print(f"  en: {ev.name_en or '(空)'}")
        print(f"  desc_zh: {ev.desc_zh or '(空)'}")
        print(f"  desc_en: {ev.desc_en or '(空)'}")

    print("\n第二次跑同一批(应全部命中缓存,0 新翻译):")
    events2 = [normalize(s) for s in samples]
    translate(events2, engine=EchoTranslator(), cache_path=cache_file)
