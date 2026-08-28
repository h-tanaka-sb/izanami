"""投稿予約スロットの自動割当（衝突回避・日跨ぎ巡回・1日上限・チャンネル別スロット）。

従来は「常にスロット先頭」だったため、毎晩量産すると昨日の予約と同時刻に重なっていた。
output/ の memo.json を走査して既予約枠を避け、空いている枠を順に返す。
"""
from __future__ import annotations

import datetime
from pathlib import Path

from . import util


def _occupied(output_dir: str, channel: str = "") -> set[tuple[str, str]]:
    """既に予約済み（未来のscheduled_publish_at or 投稿済み）の (日付, HH:MM) 集合。

    channel を指定するとそのチャンネルの分だけ数える（ch別スロット運用）。"""
    occ: set[tuple[str, str]] = set()
    now = datetime.datetime.now()
    try:
        # 旧名IZANAGI_*の予約も数える（数えないと同じ枠に二重予約される・2026-08-06改名）
        for d in [*Path(output_dir).glob("IZANAMI_*"), *Path(output_dir).glob("IZANAGI_*")]:
            memo = util.load_json(d / "memo.json", None)
            if not memo:
                continue
            if channel and (memo.get("channel") or "") != channel:
                continue
            iso = (memo.get("scheduled_publish_at") or "").strip()
            if not iso:
                continue
            try:
                dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                dt = dt.astimezone().replace(tzinfo=None)
            except Exception:
                continue
            if dt > now or (d / "uploaded.flag").exists():
                occ.add((dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")))
    except Exception:
        pass
    return occ


def next_free_slots(cfg: dict, output_dir: str, n: int = 1, channel: str = "") -> list[str]:
    """空いている予約枠を n 個返す（ISO 8601 +09:00）。

    - スロット時刻: チャンネル別 schedule_slots → 無ければ default_schedule_slots
    - 1日 max_uploads_per_day 本まで（既予約含む）
    - 現在+30分より前の枠は使わない
    """
    slots_src = (cfg.get("schedule_slots") or "").strip() or None
    if slots_src:
        slots = [s.strip() for s in slots_src.split(",") if s.strip()]
    else:
        raw = cfg.get("default_schedule_slots") or ["09:00"]
        slots = [s.strip() for s in (raw if isinstance(raw, list) else str(raw).split(","))
                 if s.strip()]
    per_day = max(1, int(cfg.get("max_uploads_per_day", 2) or 2))
    off = max(0, int(cfg.get("default_publish_offset_days", 1) or 1))
    occ = _occupied(output_dir, channel or (cfg.get("_channel_name") or ""))
    floor = datetime.datetime.now() + datetime.timedelta(minutes=30)

    out: list[str] = []
    day = datetime.date.today() + datetime.timedelta(days=off)
    for _ in range(120):  # 最大120日先まで巡回（実質無限）
        dstr = day.isoformat()
        used_today = sum(1 for (dd, _t) in occ if dd == dstr)
        for hm in slots:
            if len(out) >= n:
                return out
            if used_today >= per_day:
                break
            try:
                hh, mm = hm.split(":")
                dt = datetime.datetime.combine(day, datetime.time(int(hh), int(mm)))
            except Exception:
                continue
            if dt < floor or (dstr, hm) in occ:
                continue
            occ.add((dstr, hm))
            used_today += 1
            out.append(dt.strftime("%Y-%m-%dT%H:%M:%S+09:00"))
        day += datetime.timedelta(days=1)
    return out
