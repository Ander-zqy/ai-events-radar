#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活动雷达 · 飞书多维表格 sink
============================================================
把 NormalizedEvent 列表 upsert 进飞书多维表格审核台。

流程:
  1. 用 app_id + app_secret 换 tenant_access_token(2h 有效,自动刷新)
  2. 拉取表格现有记录的 id 字段,建「id → record_id」映射
  3. 新记录 → 批量新增;已有记录 → 批量更新(只更新变化字段)
  4. 单次批量上限 500 条,自动分批

字段映射:契约字段名 = 飞书列名(建表时已对齐,这里零转换)
日期字段:ISO 字符串 → 毫秒时间戳(飞书日期字段要求)
============================================================
"""

import os
import time
import datetime
from dataclasses import asdict
from typing import List, Dict, Optional

import requests

# ------------------------------------------------------------
# 配置(从环境变量读;GitHub Actions 里设成 Secret)
# ------------------------------------------------------------
APP_ID     = os.environ.get("FEISHU_APP_ID",     "cli_aaca7cafee381cc6")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET",  "WckJfxiHrE7QAIyYuzP3zwSmwxDJkeXU")
APP_TOKEN  = os.environ.get("FEISHU_APP_TOKEN",   "VkjtbJKvWatcYzsBPO7cdmINnSb")
TABLE_ID   = os.environ.get("FEISHU_TABLE_ID",    "tbl814XFtuyhTbHC")

BASE = "https://open.feishu.cn/open-apis"
BATCH = 500   # 飞书单次批量上限

# 日期字段:ISO → 毫秒时间戳
DATE_FIELDS = {"start_time", "end_time"}

# 这些字段不写入飞书(内部用或飞书自带)
SKIP_FIELDS = {"created_at", "updated_at", "source_url"}


# ------------------------------------------------------------
# Token 管理
# ------------------------------------------------------------
class TokenManager:
    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def get(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        r = requests.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"获取 token 失败: {d}")
        self._token = d["tenant_access_token"]
        self._expires_at = time.time() + d.get("expire", 7200)
        return self._token

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get()}",
                "Content-Type": "application/json"}


_tm = TokenManager()


# ------------------------------------------------------------
# 工具
# ------------------------------------------------------------
def _iso_to_ms(iso: str) -> Optional[int]:
    """ISO 8601 → 毫秒时间戳。解析失败返回 None。"""
    if not iso:
        return None
    try:
        # Python 3.11+ 支持带 Z 的直接解析;兼容旧版
        iso = iso.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(iso)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _to_fields(ev_dict: dict) -> dict:
    """契约 dict → 飞书字段 dict。"""
    fields = {}
    for k, v in ev_dict.items():
        if k in SKIP_FIELDS:
            continue
        if k in DATE_FIELDS:
            ms = _iso_to_ms(str(v)) if v else None
            if ms is not None:
                fields[k] = ms
        elif isinstance(v, bool):
            fields[k] = v
        elif v is not None and str(v).strip():
            fields[k] = str(v)
    return fields


# ------------------------------------------------------------
# 读取现有记录:建 id → record_id 映射
# ------------------------------------------------------------
def _fetch_existing() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    page_token = ""
    while True:
        params = {"page_size": 500, "field_names": '["id"]'}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(
            f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records",
            headers=_tm.headers(), params=params, timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"拉取记录失败: {d}")
        for rec in d["data"].get("items", []):
            eid = rec.get("fields", {}).get("id", "")
            if eid:
                mapping[str(eid)] = rec["record_id"]
        if not d["data"].get("has_more"):
            break
        page_token = d["data"].get("page_token", "")
    return mapping


# ------------------------------------------------------------
# 批量新增 / 批量更新
# ------------------------------------------------------------
def _batch_create(records: list):
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        r = requests.post(
            f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/batch_create",
            headers=_tm.headers(),
            json={"records": [{"fields": f} for f in chunk]},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"批量新增失败: {d}")
        print(f"    [sink] 新增 {len(chunk)} 条")


def _batch_update(updates: list):
    """updates: list of {"record_id": ..., "fields": {...}}"""
    for i in range(0, len(updates), BATCH):
        chunk = updates[i:i + BATCH]
        r = requests.post(
            f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/batch_update",
            headers=_tm.headers(),
            json={"records": chunk},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"批量更新失败: {d}")
        print(f"    [sink] 更新 {len(chunk)} 条")


# ------------------------------------------------------------
# 主入口
# ------------------------------------------------------------
def upsert(events) -> dict:
    """
    把 NormalizedEvent 列表 upsert 进飞书多维表格。
    返回 {"created": N, "updated": N, "skipped": N}
    """
    from normalize import NormalizedEvent
    dicts = [asdict(e) if isinstance(e, NormalizedEvent) else e for e in events]

    print(f"    [sink] 拉取现有记录...")
    existing = _fetch_existing()
    print(f"    [sink] 表格现有 {len(existing)} 条")

    to_create, to_update = [], []
    for d in dicts:
        eid = d.get("id", "")
        fields = _to_fields(d)
        if not eid or not fields:
            continue
        if eid in existing:
            to_update.append({"record_id": existing[eid], "fields": fields})
        else:
            to_create.append(fields)

    if to_create:
        _batch_create(to_create)
    if to_update:
        _batch_update(to_update)

    skipped = len(dicts) - len(to_create) - len(to_update)
    print(f"    [sink] 完成:新增 {len(to_create)} / 更新 {len(to_update)} / 跳过 {skipped}")
    return {"created": len(to_create), "updated": len(to_update), "skipped": skipped}


# ============================================================
# 快速验证:写一条假数据进表,确认四件套+权限都通
# ============================================================
if __name__ == "__main__":
    import json

    print("① 换 token...")
    tok = _tm.get()
    print(f"   tenant_access_token: {tok[:12]}...  ✓")

    print("\n② 写一条测试记录...")
    test_fields = {
        "id": "_smoke_test_001",
        "name_zh": "【测试】请删除",
        "name_en": "Smoke Test - Delete Me",
        "status": "pending",
        "source": "luma",
        "lang": "en",
        "is_online": True,
        "register_url": "https://example.com",
        "topic_tags": "AI, 测试",
    }
    r = requests.post(
        f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records",
        headers=_tm.headers(),
        json={"fields": test_fields},
        timeout=15,
    )
    d = r.json()
    if d.get("code") == 0:
        rid = d["data"]["record"]["record_id"]
        print(f"   写入成功,record_id: {rid}  ✓")
        print("   → 去飞书表格里确认有一条「【测试】请删除」,然后手动删掉它")
    else:
        print(f"   ✗ 写入失败: {json.dumps(d, ensure_ascii=False)}")
        print("\n   常见原因:")
        print("   91403 → 第四步没做:表格右上角「...」→「添加文档应用」")
        print("   99991663 → 应用未发布或可用范围没包含你自己")
        print("   1254281 → 单选字段的选项值不存在(status/source/lang 选项没填)")
