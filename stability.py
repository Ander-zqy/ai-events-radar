#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活动雷达 · stability 层
============================================================
让管道扛得住每天无人值守地跑。提供:

  safe_get(...)     —— requests.get 的健壮替换:重试 + 指数退避 +
                       429 限速识别(尊重 Retry-After)+ 按域名礼貌间隔。
                       最终失败时抛原异常,与「get + raise_for_status」语义兼容。
  SourceHealth      —— 源健康哨兵:记录各源历史产量(滚动中位数基线),
                       今天归零/暴跌就告警,抓住 HTML 源静默失效。
  RunReport         —— 一次运行的可观测摘要(各源条数/耗时/状态/异常)。
  run_source(...)   —— 包一层跑某个源:计时、捕异常、查哨兵、记报告。
依赖:requests
============================================================
"""

import time
import json
import random
import datetime as dt
from statistics import median
from urllib.parse import urlsplit
from typing import Callable, List, Optional, Tuple

import requests

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MIN_INTERVAL = 1.0          # 同域名两次请求最小间隔(秒);测试可设 0
_last_hit: dict = {}        # host -> 上次请求时间戳


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except Exception:
        return ""


def _respect_rate(url: str, min_interval: float):
    h = _host(url)
    wait = min_interval - (time.time() - _last_hit.get(h, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[h] = time.time()


class HttpError(Exception):
    pass


def safe_get(url, params=None, headers=None, timeout: int = 20,
             retries: int = 3, min_interval: Optional[float] = None,
             backoff: float = 1.0):
    """健壮 GET。2xx/3xx/4xx(非429)直接返回 Response(交调用方 raise_for_status);
       429/5xx/网络错 → 退避重试;重试用尽抛异常。"""
    mi = MIN_INTERVAL if min_interval is None else min_interval
    hdrs = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        hdrs.update(headers)

    last_exc = None
    for attempt in range(retries + 1):
        _respect_rate(url, mi)
        try:
            r = requests.get(url, params=params, headers=hdrs, timeout=timeout)
        except Exception as e:                      # 网络层错误 → 重试
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, 0.4))
                continue
            raise

        code = r.status_code
        if code == 429 or 500 <= code < 600:        # 限速 / 服务端错 → 重试
            ra = r.headers.get("Retry-After", "")
            delay = (float(ra) if str(ra).isdigit()
                     else backoff * (2 ** attempt) + random.uniform(0, 0.4))
            last_exc = HttpError(f"HTTP {code} on {url}")
            if attempt < retries:
                time.sleep(delay)
                continue
            r.raise_for_status()
        return r                                    # 正常(或 4xx 交调用方处理)

    if last_exc:
        raise last_exc


# ------------------------------------------------------------
# 源健康哨兵
# ------------------------------------------------------------
class SourceHealth:
    WINDOW = 7          # 用最近 7 次的中位数当基线
    LOW_RATIO = 0.4     # 低于基线 40% 视为暴跌

    def __init__(self, path: str = ".source_health.json"):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self._d = json.load(f)
        except Exception:
            self._d = {}

    def check(self, name: str, count: int) -> Tuple[str, Optional[int]]:
        """返回 (状态, 基线)。状态 ∈ new / ok / low / zero。"""
        hist = self._d.get(name, [])
        base = int(median(hist)) if hist else None
        if base is None:
            status = "new"
        elif count == 0 and base >= 1:
            status = "zero"
        elif count < max(1, base * self.LOW_RATIO):
            status = "low"
        else:
            status = "ok"
        # 更新滚动窗口(归零也记入,持续异常才会拉低基线)
        hist.append(count)
        self._d[name] = hist[-self.WINDOW:]
        return status, base

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._d, f, ensure_ascii=False)


# ------------------------------------------------------------
# 运行报告
# ------------------------------------------------------------
_STATUS_ICON = {"ok": "✓", "new": "•", "low": "⚠", "zero": "✗"}


class RunReport:
    def __init__(self):
        self.started = dt.datetime.now(dt.timezone.utc)
        self.t0 = time.time()
        self.rows: List[Tuple[str, int, float, str, Optional[int]]] = []
        self.errors: List[Tuple[str, str]] = []

    def add_source(self, name, count, secs, status, base):
        self.rows.append((name, count, secs, status, base))

    def add_error(self, name, exc):
        self.errors.append((name, str(exc)))

    def warnings(self):
        return [r for r in self.rows if r[3] in ("low", "zero")]

    def ok(self) -> bool:
        """无异常、无源归零 → True(可作 CI 退出码依据)。"""
        return not self.errors and not any(r[3] == "zero" for r in self.rows)

    def summary(self) -> str:
        total = sum(r[1] for r in self.rows)
        lines = [f"抓取健康日报 · {self.started:%Y-%m-%d %H:%M UTC}",
                 f"共 {total} 条 / 用时 {time.time() - self.t0:.1f}s"]
        for name, count, secs, status, base in self.rows:
            icon = _STATUS_ICON.get(status, "?")
            base_s = f"(基线 {base})" if base is not None else "(首次)"
            note = ""
            if status == "zero":
                note = "  ← 疑似失效,去查选择器/接口"
            elif status == "low":
                note = "  ← 产量暴跌"
            lines.append(f"  {icon} {name:<10} {count:>4} 条 {base_s}{note}")
        for name, err in self.errors:
            lines.append(f"  ✗ {name:<10} 异常: {err}")
        return "\n".join(lines)


def run_source(name: str, fn: Callable, report: RunReport,
               health: SourceHealth, *args, **kwargs) -> list:
    """跑一个源:计时 + 捕异常 + 查哨兵 + 记报告。任何源失败都不影响其它源。"""
    t = time.time()
    try:
        events = fn(*args, **kwargs)
    except Exception as e:
        report.add_error(name, e)
        print(f"    [{name}] 整体失败,已跳过: {e}")
        return []
    status, base = health.check(name, len(events))
    report.add_source(name, len(events), time.time() - t, status, base)
    return events


# ============================================================
# 自测:重试 / 429 / 哨兵 / 报告
# ============================================================
if __name__ == "__main__":
    import sys
    MIN_INTERVAL = 0          # 测试不等待

    class FakeResp:
        def __init__(self, code=200, headers=None):
            self.status_code = code
            self.headers = headers or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise HttpError(f"HTTP {self.status_code}")
        def json(self): return {}
        text = ""

    print("① safe_get 重试:连续两次网络错后成功")
    calls = {"n": 0}
    def flaky_get(url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("boom")
        return FakeResp(200)
    requests.get = flaky_get
    r = safe_get("https://x.test/a", retries=3, backoff=0.01)
    print(f"   成功,共尝试 {calls['n']} 次 → 状态 {r.status_code}")

    print("\n② safe_get 处理 429:先限速后成功")
    calls["n"] = 0
    def rate_then_ok(url, **kw):
        calls["n"] += 1
        return FakeResp(429, {"Retry-After": "0"}) if calls["n"] == 1 else FakeResp(200)
    requests.get = rate_then_ok
    r = safe_get("https://x.test/b", retries=3, backoff=0.01)
    print(f"   成功,共尝试 {calls['n']} 次 → 状态 {r.status_code}")

    print("\n③ 源健康哨兵:")
    h = SourceHealth(path="/tmp/_health_demo.json")
    import os
    if os.path.exists("/tmp/_health_demo.json"):
        os.remove("/tmp/_health_demo.json")
        h = SourceHealth(path="/tmp/_health_demo.json")
    # 喂三天正常历史
    for c in (40, 42, 38):
        h.check("活动行", c)
    for name, count in [("活动行", 41), ("活动行", 5), ("活动行", 0), ("新源", 12)]:
        status, base = h.check(name, count)
        print(f"   {name} 今日 {count:>3} 条 (基线 {base}) → {status}")

    print("\n④ 运行报告:")
    rep = RunReport()
    rep.add_source("Luma", 50, 3.2, "ok", 48)
    rep.add_source("活动行", 0, 1.1, "zero", 40)
    rep.add_source("联谱", 6, 2.0, "low", 30)
    rep.add_error("Devpost", "ConnectionError: timed out")
    print(rep.summary())
    print(f"\n   report.ok() = {rep.ok()}  (有源归零/异常 → CI 应判失败)")
