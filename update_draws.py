#!/usr/bin/env python3
"""동행복권 최신 회차를 data/seed_draws.json에 반영하는 스크립트.

매주 토요일 추첨(20:35경) 이후에 실행하면 마지막으로 저장된 회차 다음부터
최신 회차까지를 동행복권 API에서 받아 시드 데이터에 합칩니다.

실행:
    python3 update_draws.py

출력:
    ADDED: 1251            새로 추가된 회차(여러 개면 쉼표로 나열)
    UP_TO_DATE: 1251       이미 최신일 때(저장된 마지막 회차)

앱(app.py)이 쓰는 것과 같은 동행복권 엔드포인트·응답 형식을 그대로 따르며,
중복 회차는 하나로 합치고 회차 오름차순으로 정렬해 UTF-8 JSON으로 저장합니다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LOTTO_DATA_DIR") or (BASE_DIR / "data"))
SEED_PATH = DATA_DIR / "seed_draws.json"

API_BASE = "https://www.dhlottery.co.kr"
API_LATEST = f"{API_BASE}/lt645/selectPstLt645Info.do"     # 최신 1회차
API_CENTER = f"{API_BASE}/lt645/selectPstLt645InfoNew.do"  # 지정 회차 ±10회차
API_ALL = API_LATEST                                       # ?srchLtEpsd=all → 전 회차
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": f"{API_BASE}/lt645/result",
}
TIMEOUT = (3.5, 15.0)


# ------------------------------------------------------------ 데이터 변환 ----
def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_record(item: dict) -> dict | None:
    """API 응답 1건 → app.py와 같은 표준 형식. 형식이 어긋나면 None."""
    if not isinstance(item, dict):
        return None
    epsd = _to_int(item.get("ltEpsd")) or _to_int(item.get("drwNo"))
    if not epsd or epsd < 1:
        return None
    raw = [item.get(f"tm{i}WnNo") if f"tm{i}WnNo" in item else item.get(f"drwtNo{i}")
           for i in range(1, 7)]
    nums = [_to_int(v) for v in raw]
    if any(n is None or not 1 <= n <= 45 for n in nums) or len(set(nums)) != 6:
        return None
    ymd = str(item.get("ltRflYmd") or item.get("drwNoDate") or "")
    if re.fullmatch(r"\d{8}", ymd):
        date_str = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", ymd):
        date_str = ymd
    else:
        date_str = None
    return {
        "drwNo": epsd,
        "date": date_str,
        "numbers": sorted(nums),
        "bonus": _to_int(item.get("bnsWnNo") or item.get("bnusNo")),
        "firstWinners": _to_int(item.get("rnk1WnNope") or item.get("firstPrzwnerCo")),
        "firstPrize": _to_int(item.get("rnk1WnAmt") or item.get("firstWinamnt")),
        "totalSales": _to_int(item.get("wholEpsdSumNtslAmt") or item.get("totSellamnt")),
    }


def _is_valid(d: dict) -> bool:
    try:
        return (isinstance(d.get("drwNo"), int) and d["drwNo"] >= 1
                and isinstance(d.get("numbers"), list) and len(d["numbers"]) == 6
                and all(isinstance(n, int) and 1 <= n <= 45 for n in d["numbers"])
                and len(set(d["numbers"])) == 6)
    except Exception:
        return False


def _dedupe_sorted(draws) -> list[dict]:
    best: dict[int, dict] = {}
    for d in draws or []:
        if isinstance(d, dict) and _is_valid(d):
            d = dict(d)
            d["numbers"] = sorted(d["numbers"])
            best[d["drwNo"]] = d
    return [best[k] for k in sorted(best)]


def _records_from(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("records", "draws", "data", "list"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def load_seed():
    """(전체 payload, 정리된 회차 목록) — 파일이 없으면 (None, [])."""
    try:
        with open(SEED_PATH, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, ValueError):
        return None, []
    return payload, _dedupe_sorted(_records_from(payload))


def save_seed(payload, draws: list[dict]) -> None:
    """원래 파일 구조를 유지하며(리스트 또는 {"draws": [...]}) UTF-8로 저장."""
    if isinstance(payload, dict):
        for key in ("draws", "records"):
            if isinstance(payload.get(key), list):
                payload[key] = draws
                break
        out = payload
    else:
        out = draws
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SEED_PATH.with_suffix(SEED_PATH.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    os.replace(tmp, SEED_PATH)


# -------------------------------------------------------------- API 조회 ----
def fetch_records(url: str, params: dict | None = None) -> list[dict]:
    """동행복권 JSON 조회. 재시도 1회, 실패 시 RuntimeError(사유)."""
    last_error = "알 수 없는 오류"
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            if "json" not in (resp.headers.get("Content-Type") or "").lower():
                raise RuntimeError("JSON 대신 HTML 응답(접속 대기/차단 화면 가능성)")
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            items = (data or {}).get("list") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise RuntimeError("응답 형식이 예상과 다름")
            return items
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                time.sleep(0.8)
    raise RuntimeError(last_error)


def fetch_latest() -> dict:
    recs = [normalize_record(x) for x in fetch_records(API_LATEST)]
    recs = [r for r in recs if r]
    if not recs:
        raise RuntimeError("최신 회차 응답에 유효한 데이터가 없습니다")
    return max(recs, key=lambda r: r["drwNo"])


def fetch_rounds_around(round_no: int) -> list[dict]:
    recs = [normalize_record(x)
            for x in fetch_records(API_CENTER, {"srchDir": "center", "srchLtEpsd": round_no})]
    return [r for r in recs if r]


def fetch_all_rounds() -> list[dict]:
    recs = [normalize_record(x) for x in fetch_records(API_ALL, {"srchLtEpsd": "all"})]
    recs = [r for r in recs if r]
    if not recs:
        raise RuntimeError("전체 회차 응답이 비어 있습니다")
    return _dedupe_sorted(recs)


def collect_missing(first: int, last: int) -> tuple[list[dict], list[int]]:
    """first~last 회차를 모은다. (받은 회차들, 끝내 못 받은 회차들)"""
    got: dict[int, dict] = {}
    pending = set(range(first, last + 1))
    while pending:
        target = min(pending)
        try:
            recs = fetch_rounds_around(target)
        except RuntimeError as exc:
            print(f"  · {target}회차 묶음 조회 실패: {exc}", file=sys.stderr)
            recs = []
        gained = {r["drwNo"]: r for r in recs if r["drwNo"] in pending}
        if not gained:
            break
        got.update(gained)
        pending -= set(gained)
    if pending:
        # 중앙 조회로 메우지 못하면 전 회차를 한 번에 받아 채운다.
        print(f"  · {len(pending)}개 회차를 전 회차 조회로 보완합니다", file=sys.stderr)
        try:
            for r in fetch_all_rounds():
                if r["drwNo"] in pending:
                    got[r["drwNo"]] = r
                    pending.discard(r["drwNo"])
        except RuntimeError as exc:
            print(f"  · 전 회차 조회 실패: {exc}", file=sys.stderr)
    return _dedupe_sorted(got.values()), sorted(pending)


# ------------------------------------------------------------------ 실행 ----
def main() -> int:
    payload, draws = load_seed()
    if payload is None:
        print(f"ERROR: {SEED_PATH} 를 읽지 못했습니다(파일 없음 또는 JSON 손상).",
              file=sys.stderr)
        return 1

    last_saved = draws[-1]["drwNo"] if draws else 0
    try:
        latest = fetch_latest()
    except RuntimeError as exc:
        print(f"ERROR: 동행복권 서버에서 최신 회차를 받지 못했습니다 ({exc})",
              file=sys.stderr)
        return 1

    if latest["drwNo"] <= last_saved:
        print(f"UP_TO_DATE: {last_saved}")
        return 0

    new_recs, missing = collect_missing(last_saved + 1, latest["drwNo"])
    if not new_recs:
        print(f"ERROR: {last_saved + 1}~{latest['drwNo']}회차 데이터를 받지 못했습니다.",
              file=sys.stderr)
        return 1

    merged = _dedupe_sorted(list(draws) + list(new_recs))
    try:
        save_seed(payload, merged)
    except OSError as exc:
        print(f"ERROR: {SEED_PATH} 저장 실패 ({exc})", file=sys.stderr)
        return 1

    added = [r["drwNo"] for r in new_recs]
    print("ADDED: " + ", ".join(str(n) for n in added))
    for r in new_recs:
        balls = ", ".join(f"{n:2d}" for n in r["numbers"])
        bonus = f" / 보너스 {r['bonus']}" if r.get("bonus") else ""
        print(f"  {r['drwNo']}회차 ({r.get('date') or '날짜 미상'})  {balls}{bonus}")
    print(f"  → {SEED_PATH.relative_to(BASE_DIR)} 저장 완료 (총 {len(merged)}회차)")
    if missing:
        print(f"  ⚠ 받지 못한 회차: {', '.join(map(str, missing))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
