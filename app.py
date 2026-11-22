"""🎱 로또 6/45 분석·조합·추첨 스튜디오

동행복권 회차별 당첨번호를 수집·캐시하고, 역대 통계 분석 / 조건 필터 조합 /
비너스 추첨기 방식 시각 추첨 / 매일 추천 기록 관리를 제공하는 Streamlit 앱.

실행:  streamlit run app.py
"""

from __future__ import annotations

import bisect
import json
import os
import random
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

# ------------------------------------------------------------------ 설정 ----
KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LOTTO_DATA_DIR") or (BASE_DIR / "data"))
DRAWS_PATH = DATA_DIR / "draws.json"
SEED_PATH = DATA_DIR / "seed_draws.json"
HISTORY_PATH = DATA_DIR / "history.json"
META_PATH = DATA_DIR / "meta.json"

API_BASE = "https://www.dhlottery.co.kr"
API_LATEST = f"{API_BASE}/lt645/selectPstLt645Info.do"          # 최신 1회차
API_CENTER = f"{API_BASE}/lt645/selectPstLt645InfoNew.do"       # 지정 회차 ±10회차
API_ALL = API_LATEST                                            # ?srchLtEpsd=all → 전 회차
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": f"{API_BASE}/lt645/result",
}
TIMEOUT = (3.5, 9.0)

TOTAL_COMBOS = 8_145_060
NUMBERS = list(range(1, 46))
BALL_COLORS = {1: "#fbc400", 2: "#69c8f2", 3: "#ff7272", 4: "#aaaaaa", 5: "#b0d840"}


def ball_color(n: int) -> str:
    """한국 로또 공 색상 구간(1~10 노랑 … 41~45 초록)."""
    return BALL_COLORS[(n - 1) // 10 + 1]


def now_kst() -> datetime:
    return datetime.now(KST)


# -------------------------------------------------------------- 데이터 계층 ----
def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_record(item: dict) -> dict | None:
    """API 응답 1건 → 앱 내부 표준 형식. 형식이 어긋나면 None."""
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


def load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


def save_json(path: Path, payload) -> bool:
    """임시 파일에 쓴 뒤 교체해 파일이 깨지지 않게 저장."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _records_from(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("records", "draws", "data", "list"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def fetch_records(url: str, params: dict | None = None,
                  timeout: tuple[float, float] | None = None) -> list[dict]:
    """동행복권 JSON 조회. 재시도 1회, 실패 시 RuntimeError(사유)."""
    last_error = "알 수 없는 오류"
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=HEADERS,
                                timeout=timeout or TIMEOUT)
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
        except Exception as exc:  # 네트워크·JSON·형식 오류 모두 여기로
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                time.sleep(0.6)
    raise RuntimeError(last_error)


def fetch_latest(timeout: tuple[float, float] | None = None) -> dict:
    recs = [normalize_record(x) for x in fetch_records(API_LATEST, timeout=timeout)]
    recs = [r for r in recs if r]
    if not recs:
        raise RuntimeError("최신 회차 응답에 유효한 데이터가 없습니다")
    return max(recs, key=lambda r: r["drwNo"])


def fetch_rounds_around(round_no: int) -> list[dict]:
    params = {"srchDir": "center", "srchLtEpsd": round_no}
    recs = [normalize_record(x) for x in fetch_records(API_CENTER, params)]
    return [r for r in recs if r]


def fetch_all_rounds() -> list[dict]:
    recs = [normalize_record(x) for x in fetch_records(API_ALL, {"srchLtEpsd": "all"})]
    recs = [r for r in recs if r]
    if not recs:
        raise RuntimeError("전체 회차 응답이 비어 있습니다")
    return _dedupe_sorted(recs)


def seed_draws() -> list[dict]:
    payload = load_json(SEED_PATH)
    return _dedupe_sorted(_records_from(payload))


@st.cache_data(show_spinner=False)
def seed_count() -> int:
    """내장 데이터 회차 수(사이드바 표시용, 한 번만 계산)."""
    return len(seed_draws())


def load_draws() -> tuple[list[dict], str]:
    """캐시 → 내장 시드 → 원격 전체 수집 순으로 확보. (draws, source)"""
    payload = load_json(DRAWS_PATH)
    draws = _dedupe_sorted(_records_from(payload))
    if draws:
        return draws, "cache"
    draws = seed_draws()
    if draws:
        save_json(DRAWS_PATH, draws)
        return draws, "seed"
    try:
        draws = fetch_all_rounds()
    except RuntimeError:
        return [], "empty"
    save_json(DRAWS_PATH, draws)
    return draws, "network"


def update_draws(draws: list[dict]) -> tuple[list[dict], str, str]:
    """최신 회차만 증분 수집. (draws, level, message)"""
    if not draws:
        try:
            fresh = fetch_all_rounds()
        except RuntimeError as exc:
            return draws, "error", (
                "네트워크에서 데이터를 받지 못했고 내장 데이터도 없습니다. "
                f"인터넷 연결을 확인한 뒤 다시 시도해 주세요. ({exc})"
            )
        save_json(DRAWS_PATH, fresh)
        return fresh, "success", f"전체 {len(fresh)}회차를 새로 받았습니다."
    latest_cached = draws[-1]["drwNo"]
    try:
        rec = fetch_latest()
    except RuntimeError as exc:
        return draws, "error", (
            "동행복권 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요. "
            f"캐시된 {latest_cached}회차 데이터로 계속 사용할 수 있습니다. ({exc})"
        )
    if rec["drwNo"] <= latest_cached:
        return draws, "info", f"이미 최신입니다 (최신 {latest_cached}회차)."
    gap = rec["drwNo"] - latest_cached
    if gap == 1:
        merged = _dedupe_sorted(draws + [rec])
    elif gap <= 10:
        # 최근 10회차 묶음을 받아 비어 있는 회차를 함께 채운다
        try:
            extra = fetch_rounds_around(rec["drwNo"])
        except RuntimeError:
            extra = []
        merged = _dedupe_sorted(draws + [rec] + extra)
    else:
        try:
            merged = _dedupe_sorted(draws + fetch_all_rounds())
        except RuntimeError:
            merged = _dedupe_sorted(draws + [rec])
            if not save_json(DRAWS_PATH, merged):
                return merged, "warning", (
                    f"{rec['drwNo']}회차를 받았지만 데이터 파일에 저장하지 못했습니다."
                )
            return merged, "warning", (
                f"{rec['drwNo']}회차를 추가했습니다. 다만 그 사이 {gap - 1}개 회차는 "
                "중간 데이터를 받지 못해 비어 있을 수 있습니다. '전체 데이터 다시 받기'를 한 번 실행해 주세요."
            )
    if not save_json(DRAWS_PATH, merged):
        return merged, "warning", f"{rec['drwNo']}회차를 받았지만 파일 저장에 실패했습니다(권한 확인)."
    return merged, "success", f"{rec['drwNo']}회차({rec.get('date') or '날짜 미상'})를 추가했습니다."


def refresh_all(draws: list[dict]) -> tuple[list[dict], str, str]:
    try:
        fresh = fetch_all_rounds()
    except RuntimeError as exc:
        return draws, "error", f"전체 데이터를 받지 못했습니다. 잠시 후 다시 시도해 주세요. ({exc})"
    if not save_json(DRAWS_PATH, fresh):
        return fresh, "warning", "데이터는 받았지만 파일 저장에 실패했습니다(권한 확인)."
    return fresh, "success", f"전체 {len(fresh)}회차를 새로 받았습니다 (최신 {fresh[-1]['drwNo']}회차)."


def auto_sync(draws: list[dict]) -> tuple[list[dict], str | None]:
    """앱 실행 시 최신 회차를 조용히 확인한다(1시간에 1회, 실패는 10분간 재시도 안 함).

    반환: (draws, 안내문 또는 None)
    """
    if os.environ.get("LOTTO_NO_AUTOSYNC") == "1" or not draws:
        return draws, None
    meta = load_json(META_PATH) or {}
    now = time.time()
    try:
        if now - float(meta.get("last_sync_ts") or 0) < 3600:
            return draws, None
        if now - float(meta.get("last_attempt_ts") or 0) < 600:
            return draws, None
    except (TypeError, ValueError):
        pass

    meta["last_attempt_ts"] = now
    save_json(META_PATH, meta)
    try:
        rec = fetch_latest(timeout=(2.0, 5.0))
    except RuntimeError:
        return draws, "auto_failed"

    merged = draws
    if rec["drwNo"] > draws[-1]["drwNo"]:
        if rec["drwNo"] - draws[-1]["drwNo"] == 1:
            merged = _dedupe_sorted(draws + [rec])
        else:
            try:
                merged = _dedupe_sorted(draws + fetch_all_rounds())
            except RuntimeError:
                merged = _dedupe_sorted(draws + [rec])
        save_json(DRAWS_PATH, merged)
        meta["last_sync_ts"] = now
        save_json(META_PATH, meta)
        return merged, f"새 회차 {rec['drwNo']}회를 자동으로 받았습니다."
    meta["last_sync_ts"] = now
    save_json(META_PATH, meta)
    return merged, None


# ---------------------------------------------------------------- 기록 계층 ----
# 기록은 두 곳에 저장될 수 있다.
#   1) 로컬 실행(동철님 PC)      → data/history.json      (기본값)
#   2) Streamlit Cloud 배포       → 구글 시트 웹 앱(GSHEET_WEBAPP_URL + GSHEET_TOKEN)
# 시트 모드에서는 위젯 조작마다 네트워크를 왕복하지 않도록 session_state에 캐시한다.
HISTORY_COLUMNS = ["created_at", "target_round", "mode", "numbers",
                   "score_json", "filters_json"]
HISTORY_CACHE_KEY = "history_cache"
HISTORY_CACHE_TTL = 60.0        # 캐시 유효 시간(초)
HISTORY_ERROR_KEY = "history_error"


def _setting(name: str) -> str:
    """st.secrets → 환경변수 순서로 설정값을 찾는다(테스트·로컬 실행 대비)."""
    try:
        value = st.secrets.get(name)      # type: ignore[attr-defined]
    except Exception:
        value = None
    if value:
        return str(value).strip()
    return str(os.environ.get(name) or "").strip()


def gsheet_config() -> tuple[str, str]:
    """(웹 앱 URL, 비밀 토큰). URL이 비어 있으면 로컬 파일 모드."""
    return _setting("GSHEET_WEBAPP_URL"), _setting("GSHEET_TOKEN")


def storage_mode() -> str:
    return "gsheet" if gsheet_config()[0] else "local"


def storage_label() -> str:
    return "구글 시트 연동 중" if storage_mode() == "gsheet" else "로컬 파일"


def storage_target() -> str:
    return "구글 시트 '로또 분석기 추천 기록'" if storage_mode() == "gsheet" else str(HISTORY_PATH)


def save_error_text() -> str:
    if storage_mode() == "gsheet":
        return "구글 시트에 반영하지 못했습니다"
    return "기록 파일에 저장하지 못했습니다(권한 확인)"


def _gsheet_call(method: str, payload: dict | None = None) -> dict:
    """Apps Script 웹 앱 호출. 웹 앱은 302 리다이렉트로 응답하므로
    requests가 리다이렉트를 따라가 결과 JSON을 읽는다."""
    url, token = gsheet_config()
    if not url:
        raise RuntimeError("GSHEET_WEBAPP_URL 이 설정되지 않았습니다.")
    body = {"token": token}
    if payload:
        body.update(payload)
    try:
        if method.upper() == "GET":
            resp = requests.get(url, params={"token": token},
                                timeout=(4.0, 25.0), allow_redirects=True)
        else:
            resp = requests.post(url, json=body, timeout=(4.0, 45.0),
                                 allow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:                  # 네트워크·JSON 오류 모두 방어
        raise RuntimeError(f"구글 시트 연결 실패({exc})") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        reason = data.get("error") if isinstance(data, dict) else "응답 형식 오류"
        raise RuntimeError(f"구글 시트 응답 오류({reason})")
    return data


def _normalize_history(recs) -> list[dict]:
    """저장 방식과 무관하게 쓰는 공통 정리·검증."""
    out = []
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        nums = r.get("numbers")
        if not (isinstance(nums, list) and len(nums) == 6):
            continue
        try:
            nums = sorted(int(n) for n in nums)
        except (TypeError, ValueError):
            continue
        if any(not 1 <= n <= 45 for n in nums) or len(set(nums)) != 6:
            continue
        rr = dict(r)
        rr["numbers"] = nums
        rr["score"] = rr.get("score") or {}
        rr["filters"] = rr.get("filters") or {}
        if rr.get("target_round") is not None:
            try:
                rr["target_round"] = int(rr["target_round"])
            except (TypeError, ValueError):
                rr["target_round"] = None
        if not rr.get("id"):
            tail = "-".join(f"{n:02d}" for n in nums)
            rr["id"] = f"{rr.get('created_at') or ''}-{tail}"
        out.append(rr)
    out.sort(key=lambda x: str(x.get("created_at") or ""))
    return out


def sheet_rows(records: list[dict]) -> list[dict]:
    """시트에 쓸 6개 열만 추린다(헤더: HISTORY_COLUMNS)."""
    rows = []
    for r in records:
        rows.append({
            "created_at": r.get("created_at"),
            "target_round": r.get("target_round"),
            "mode": r.get("mode"),
            "numbers": list(r.get("numbers") or []),
            "score": r.get("score") or {},
            "filters": r.get("filters") or {},
        })
    return rows


def load_history(force: bool = False) -> list[dict]:
    """추천 기록을 읽는다. 시트 모드는 세션 캐시를 먼저 쓰고,
    실패하면 캐시로 계속 동작한다(앱은 죽지 않는다)."""
    if storage_mode() == "gsheet":
        cache = st.session_state.get(HISTORY_CACHE_KEY)
        now = time.time()
        if not force and cache and now - float(cache.get("ts") or 0) < HISTORY_CACHE_TTL:
            return [dict(r) for r in cache.get("records") or []]
        try:
            data = _gsheet_call("GET")
            records = _normalize_history(data.get("records"))
        except RuntimeError as exc:
            st.session_state[HISTORY_ERROR_KEY] = str(exc)
            if cache:
                return [dict(r) for r in cache.get("records") or []]
            return []
        st.session_state[HISTORY_ERROR_KEY] = None
        st.session_state[HISTORY_CACHE_KEY] = {"ts": now, "records": records}
        return [dict(r) for r in records]

    payload = load_json(HISTORY_PATH)
    return _normalize_history(_records_from(payload))


def save_history(records: list[dict]) -> bool:
    """기록 전체를 저장한다. 성공하면 세션 캐시도 함께 갱신한다."""
    if storage_mode() == "gsheet":
        try:
            _gsheet_call("POST", {"records": sheet_rows(records)})
        except RuntimeError as exc:
            st.session_state[HISTORY_ERROR_KEY] = str(exc)
            return False
        st.session_state[HISTORY_ERROR_KEY] = None
        st.session_state[HISTORY_CACHE_KEY] = {
            "ts": time.time(), "records": _normalize_history(records)}
        return True
    return save_json(HISTORY_PATH, {"version": 1, "records": records})


def history_sets(records: list[dict]) -> set[tuple[int, ...]]:
    return {tuple(sorted(r["numbers"])) for r in records}


# ---------------------------------------------------------------- 통계 계층 ----
def build_frame(draws: list[dict]) -> pd.DataFrame:
    rows = []
    for d in draws:
        nums = d["numbers"]
        rows.append({
            "회차": d["drwNo"],
            "추첨일": d.get("date"),
            "번호": nums,
            "n1": nums[0], "n2": nums[1], "n3": nums[2],
            "n4": nums[3], "n5": nums[4], "n6": nums[5],
            "보너스": d.get("bonus"),
            "홀수개수": sum(n % 2 for n in nums),
            "저번호개수": sum(n <= 22 for n in nums),
            "합계": sum(nums),
            "연속": consecutive_max(nums),
            "1등당첨자수": d.get("firstWinners"),
            "1등당첨금": d.get("firstPrize"),
        })
    return pd.DataFrame(rows)


def consecutive_max(nums) -> int:
    best = run = 1
    for i in range(1, len(nums)):
        if nums[i] == nums[i - 1] + 1:
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def number_frequency(df: pd.DataFrame, include_bonus: bool = False) -> pd.Series:
    counts = Counter()
    for nums in df["번호"]:
        counts.update(nums)
    if include_bonus:
        counts.update(int(b) for b in df["보너스"].dropna())
    return pd.Series({n: counts.get(n, 0) for n in NUMBERS}, name="출현횟수")


def recent_window(df: pd.DataFrame, window: int) -> pd.DataFrame:
    tail = df.tail(window)
    counts = number_frequency(tail)
    last_seen = {}
    for _, row in df.iterrows():
        for n in row["번호"]:
            last_seen[n] = row["회차"]
    latest_round = int(df["회차"].max())
    out = pd.DataFrame({
        "번호": NUMBERS,
        "최근출현": [int(counts[n]) for n in NUMBERS],
        "마지막출현회차": [last_seen.get(n) for n in NUMBERS],
    })
    out["경과회차"] = out["마지막출현회차"].apply(
        lambda r: latest_round - int(r) if pd.notna(r) else latest_round
    )
    return out


def distribution_table(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    total = max(len(df), 1)
    odd = df["홀수개수"].value_counts().reindex(range(7), fill_value=0)
    low = df["저번호개수"].value_counts().reindex(range(7), fill_value=0)
    bins = list(range(20, 261, 10))
    sums = pd.cut(df["합계"], bins=bins, right=False,
                  labels=[f"{b}~{b + 9}" for b in bins[:-1]])
    sum_counts = sums.value_counts().reindex([f"{b}~{b + 9}" for b in bins[:-1]], fill_value=0)
    return {
        "odd": pd.DataFrame({"홀수개수": odd.index, "회차수": odd.values,
                             "비율": (odd.values / total * 100).round(1)}),
        "low": pd.DataFrame({"저번호개수": low.index, "회차수": low.values,
                             "비율": (low.values / total * 100).round(1)}),
        "sum": pd.DataFrame({"합계구간": sum_counts.index, "회차수": sum_counts.values,
                             "비율": (sum_counts.values / total * 100).round(1),
                             "구간시작": bins[:-1]}),
    }


def build_profile(df: pd.DataFrame) -> dict:
    """조합 적합도 계산용 역대 분포 프로필."""
    sums = df["합계"].astype(int).tolist()
    sorted_sums = sorted(sums)
    density = {}
    for s in range(6, 271):
        lo = s - 12
        hi = s + 12
        # |x - s| <= 12 인 회차 수 (이진 탐색)
        density[s] = bisect.bisect_right(sorted_sums, hi) - bisect.bisect_left(sorted_sums, lo)
    max_density = max(density.values()) or 1
    n = max(len(df), 1)
    odd_share = (df["홀수개수"].value_counts() / n).to_dict()
    low_share = (df["저번호개수"].value_counts() / n).to_dict()
    return {
        "sum_density": density,
        "sum_max": max_density,
        "odd_share": odd_share,
        "odd_max": max(odd_share.values()) if odd_share else 1,
        "low_share": low_share,
        "low_max": max(low_share.values()) if low_share else 1,
        "n": len(df),
        "high_ratio": (sum(1 for nums in df["번호"] for n in nums if n >= 32)
                       / max(6 * len(df), 1)),
    }


def score_combo(nums, profile: dict) -> dict:
    s = sum(nums)
    odd = sum(n % 2 for n in nums)
    low = sum(n <= 22 for n in nums)
    high = sum(n >= 32 for n in nums)
    sum_score = 100.0 * profile["sum_density"].get(s, 0) / profile["sum_max"]
    odd_score = 100.0 * profile["odd_share"].get(odd, 0) / profile["odd_max"]
    low_score = 100.0 * profile["low_share"].get(low, 0) / profile["low_max"]
    fit = 0.34 * sum_score + 0.33 * odd_score + 0.33 * low_score
    return {
        "합계": s,
        "홀수": odd,
        "저번호": low,
        "합계점수": round(sum_score, 1),
        "홀짝점수": round(odd_score, 1),
        "고저점수": round(low_score, 1),
        "적합도": round(fit, 1),
        "생일회피": round(100 * high / 6),
        "연속": consecutive_max(sorted(nums)),
    }


# -------------------------------------------------------------- 조합 생성기 ----
def _category(n: int) -> str:
    return ("L" if n <= 22 else "H") + ("O" if n % 2 else "E")


def generate_combos(*, count: int, fixed, excluded, odd_range, low_range,
                    sum_range, max_consec, past_wins: set, saved_sets: set,
                    profile: dict, exclude_past: bool = True,
                    exclude_saved: bool = True, rng: random.Random | None = None,
                    max_attempts: int = 40_000):
    """조건을 만족하고 기존 조합과 겹치지 않는 조합 목록을 생성.

    반환: (combos, attempts, fail_reasons)
    """
    rng = rng or random.Random()
    fixed = sorted(set(fixed))
    excluded = set(excluded)
    if len(fixed) > 6:
        raise ValueError("고정수는 최대 5개까지 선택할 수 있습니다.")
    if set(fixed) & excluded:
        raise ValueError("같은 번호를 고정수와 제외수에 함께 넣을 수 없습니다.")

    pool = [n for n in NUMBERS if n not in excluded and n not in fixed]
    by_cat = {c: [n for n in pool if _category(n) == c] for c in ("LO", "LE", "HO", "HE")}
    f_odd = sum(n % 2 for n in fixed)
    f_low = sum(n <= 22 for n in fixed)
    k = 6 - len(fixed)

    odd_opts = [o for o in range(odd_range[0], odd_range[1] + 1) if 0 <= o - f_odd <= k]
    low_opts = [l for l in range(low_range[0], low_range[1] + 1) if 0 <= l - f_low <= k]
    if not odd_opts:
        raise ValueError("고정수를 포함하면 선택한 홀짝 조건을 만족할 수 없습니다. 조건을 넓혀 주세요.")
    if not low_opts:
        raise ValueError("고정수를 포함하면 선택한 고저 조건을 만족할 수 없습니다. 조건을 넓혀 주세요.")
    odd_w = [profile["odd_share"].get(o, 0.01) for o in odd_opts]
    low_w = [profile["low_share"].get(l, 0.01) for l in low_opts]

    fixed_set = set(fixed)
    combos: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    fails = Counter()
    attempts = 0

    while len(combos) < count and attempts < max_attempts:
        attempts += 1
        o = rng.choices(odd_opts, weights=odd_w)[0]
        l = rng.choices(low_opts, weights=low_w)[0]
        need_odd, need_low = o - f_odd, l - f_low
        # 남은 k개 중 저-홀수(x)·저-짝(y)·고-홀수(ho)·고-짝(he)
        x_lo = max(0, need_low + need_odd - k)
        x_hi = min(need_low, need_odd)
        if x_lo > x_hi:
            fails["홀짝·고저 조합 불가"] += 1
            continue
        x = rng.randint(x_lo, x_hi)
        y, ho = need_low - x, need_odd - x
        he = k - x - y - ho
        if min(x, y, ho, he) < 0:
            fails["홀짝·고저 조합 불가"] += 1
            continue
        picks = []
        ok = True
        for cat, cnt in (("LO", x), ("LE", y), ("HO", ho), ("HE", he)):
            if cnt > len(by_cat[cat]):
                ok = False
                break
            picks.extend(rng.sample(by_cat[cat], cnt))
        if not ok:
            fails["제외수로 후보 부족"] += 1
            continue
        nums = sorted(fixed + picks)
        if len(set(nums)) != 6:
            fails["중복"] += 1
            continue
        if not (sum_range[0] <= sum(nums) <= sum_range[1]):
            fails["총합 구간"] += 1
            continue
        if consecutive_max(nums) > max_consec:
            fails["연속번호"] += 1
            continue
        key = tuple(nums)
        if exclude_past and key in past_wins:
            fails["역대 1등 중복"] += 1
            continue
        if exclude_saved and key in saved_sets:
            fails["기존 추천 중복"] += 1
            continue
        if key in seen:
            fails["방금 생성 중복"] += 1
            continue
        seen.add(key)
        combos.append(nums)

    return combos, attempts, fails


# ------------------------------------------------------------------ 표시 도우미 ----
def balls_html(nums, size: int = 36) -> str:
    spans = []
    for n in nums:
        spans.append(
            f'<span style="display:inline-flex;align-items:center;justify-content:center;'
            f'width:{size}px;height:{size}px;border-radius:50%;margin:3px 4px;'
            f'background:{ball_color(n)};color:#1b1b1b;font-weight:800;'
            f'font-size:{int(size * 0.42)}px;box-shadow:inset -3px -4px 8px rgba(0,0,0,.18),'
            f'inset 3px 3px 8px rgba(255,255,255,.55);">{n}</span>'
        )
    return '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:2px">' + "".join(spans) + "</div>"


def history_rows(records: list[dict]) -> pd.DataFrame:
    rows = []
    for r in records:
        sc = r.get("score") or {}
        nums = r["numbers"]
        rows.append({
            "생성시각": r.get("created_at"),
            "대상회차": r.get("target_round"),
            "추첨일": r.get("draw_date"),
            "번호": " · ".join(f"{n:02d}" for n in nums),
            "홀:짝": f"{sum(n % 2 for n in nums)}:{6 - sum(n % 2 for n in nums)}",
            "고:저": f"{sum(n > 22 for n in nums)}:{sum(n <= 22 for n in nums)}",
            "합계": sum(nums),
            "적합도": sc.get("적합도"),
            "생일회피": sc.get("생일회피"),
            "모드": r.get("mode"),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------- 추첨 애니메이션 ----
def venus_html(order: list[int], height: int = 430) -> str:
    """비너스 추첨기 스타일 캔버스 연출.

    번호와 순서는 파이썬 난수로 이미 정해져 있고, 이 스크립트는 그 결과를
    공이 섞이고 하나씩 나오는 장면으로 보여 주기만 한다.
    (스크립트가 실행되지 않는 환경에서도 번호는 화면 아래에 그대로 표시된다.)
    """
    order_js = json.dumps([int(n) for n in order])
    return f"""
<div style="width:100%;max-width:900px;margin:0 auto">
  <canvas id="lotto-venus-canvas" width="880" height="{height}"
    style="width:100%;height:{height}px;border-radius:18px;display:block;
           background:radial-gradient(circle at 50% 30%, #1b2540 0%, #0b1020 65%, #06080f 100%);
           box-shadow:0 18px 40px rgba(0,0,0,.35)"></canvas>
</div>
<script>
(function () {{
  var ORDER = {order_js};
  var canvas = document.getElementById("lotto-venus-canvas");
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext("2d");
  var W = 0, H = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
  var cx = 0, cy = 0, R = 0, ballR = 12;
  var MIX = 1100, GAP = 620, EJECT = 470;
  var balls = [], t0 = 0, rAF = null;
  window.__lottoVenusToken = (window.__lottoVenusToken || 0) + 1;
  var token = window.__lottoVenusToken;

  function color(n) {{
    if (n <= 10) return "#fbc400";
    if (n <= 20) return "#69c8f2";
    if (n <= 30) return "#ff7272";
    if (n <= 40) return "#aaaaaa";
    return "#b0d840";
  }}

  function measure() {{
    var w = canvas.clientWidth || canvas.parentElement.clientWidth || 880;
    var h = canvas.clientHeight || {height};
    W = Math.max(320, w); H = Math.max(240, h);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = W * 0.5; cy = H * 0.68; R = Math.min(W * 0.27, H * 0.42);
    ballR = Math.max(9, Math.min(16, R * 0.078));
  }}

  function slotPos(i) {{
    var pad = Math.min(40, W * 0.06), slotW = (W - pad * 2) / 6;
    return {{ x: pad + slotW * (i + 0.5), y: Math.max(34, H * 0.1) }};
  }}

  function setup() {{
    balls = [];
    for (var i = 1; i <= 45; i++) {{
      var a = Math.random() * Math.PI * 2;
      var d = Math.sqrt(Math.random()) * Math.max(10, R - ballR - 6);
      balls.push({{
        n: i, x: cx + Math.cos(a) * d, y: cy + Math.sin(a) * d,
        vx: (Math.random() - 0.5) * 4, vy: (Math.random() - 0.5) * 4,
        ejectAt: -1, eject: false, sx: 0, sy: 0, ex: 0, ey: 0, landed: false, prog: 0
      }});
    }}
    ORDER.forEach(function (n, i) {{ if (balls[n - 1]) balls[n - 1].ejectAt = MIX + i * GAP; }});
    t0 = 0;
  }}

  function rubColor(n, x, y, r) {{
    var g = ctx.createRadialGradient(x - r * 0.35, y - r * 0.4, r * 0.15, x, y, r);
    g.addColorStop(0, "#ffffff");
    g.addColorStop(0.3, color(n));
    g.addColorStop(1, color(n));
    ctx.fillStyle = g;
  }}

  function frame(ts) {{
    if (token !== window.__lottoVenusToken) return;   // 새로 그려졌으면 이전 루프 종료
    if (!t0) {{ t0 = ts; }}
    var t = ts - t0;

    ctx.clearRect(0, 0, W, H);

    // 드럼 본체
    var drum = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.35, R * 0.2, cx, cy, R);
    drum.addColorStop(0, "rgba(140,190,255,0.18)");
    drum.addColorStop(1, "rgba(40,70,130,0.05)");
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fillStyle = drum; ctx.fill();
    ctx.lineWidth = 6; ctx.strokeStyle = "rgba(150,200,255,0.35)"; ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy, R - 14, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.07)"; ctx.lineWidth = 2; ctx.stroke();

    // 받침대
    ctx.beginPath();
    ctx.moveTo(cx - R * 0.42, cy + R * 1.02);
    ctx.lineTo(cx + R * 0.42, cy + R * 1.02);
    ctx.lineTo(cx + R * 0.24, cy + R * 1.16);
    ctx.lineTo(cx - R * 0.24, cy + R * 1.16);
    ctx.closePath(); ctx.fillStyle = "rgba(120,160,220,0.18)"; ctx.fill();

    // 결과 슬롯
    for (var s = 0; s < 6; s++) {{
      var p = slotPos(s);
      ctx.beginPath(); ctx.arc(p.x, p.y, ballR + 5, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255,255,255,0.16)"; ctx.lineWidth = 2; ctx.stroke();
    }}

    var done = MIX + 5 * GAP + EJECT;
    var spinning = t < done;

    for (var i = 0; i < balls.length; i++) {{
      var b = balls[i];
      if (b.eject) {{
        if (!b.landed) {{
          b.prog = Math.min(1, (t - b.ejectAt) / EJECT);
          var e = 1 - Math.pow(1 - b.prog, 3);
          b.x = b.sx + (b.ex - b.sx) * e;
          b.y = b.sy + (b.ey - b.sy) * e
              - Math.sin(Math.PI * b.prog) * Math.min(40, H * 0.12);  // 포물선
          if (b.prog >= 1) {{ b.landed = true; b.x = b.ex; b.y = b.ey; }}
        }}
      }} else if (b.ejectAt >= 0 && t >= b.ejectAt) {{
        var sp = slotPos(ORDER.indexOf(b.n));
        b.eject = true; b.sx = b.x; b.sy = b.y; b.ex = sp.x; b.ey = sp.y;
      }} else if (spinning) {{
        b.vx += (Math.random() - 0.5) * 0.9;
        b.vy += (Math.random() - 0.5) * 0.9;
        b.vx *= 0.985; b.vy *= 0.985;
        b.x += b.vx; b.y += b.vy;
        var dx = b.x - cx, dy = b.y - cy, dist = Math.sqrt(dx * dx + dy * dy) || 0.001;
        var lim = R - ballR - 5;
        if (dist > lim) {{
          var nx = dx / dist, ny = dy / dist;
          b.x = cx + nx * lim; b.y = cy + ny * lim;
          var dot = b.vx * nx + b.vy * ny;
          b.vx -= 2 * dot * nx; b.vy -= 2 * dot * ny;
        }}
      }}
    }}

    // 공끼리 겹치지 않게
    for (var i = 0; i < balls.length; i++) {{
      var a = balls[i]; if (a.eject) continue;
      for (var j = i + 1; j < balls.length; j++) {{
        var c = balls[j]; if (c.eject) continue;
        var ddx = c.x - a.x, ddy = c.y - a.y;
        var dd = Math.sqrt(ddx * ddx + ddy * ddy) || 0.001;
        if (dd < ballR * 2) {{
          var push = (ballR * 2 - dd) / 2, ux = ddx / dd, uy = ddy / dd;
          a.x -= ux * push; a.y -= uy * push;
          c.x += ux * push; c.y += uy * push;
        }}
      }}
    }}

    for (var i = 0; i < balls.length; i++) {{
      var b2 = balls[i];
      rubColor(b2.n, b2.x, b2.y, ballR);
      ctx.beginPath(); ctx.arc(b2.x, b2.y, ballR, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = "rgba(0,0,0,0.18)"; ctx.lineWidth = 1; ctx.stroke();
      ctx.fillStyle = "#1b1b1b";
      ctx.font = "700 " + Math.round(ballR * 0.95) + "px system-ui, sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(String(b2.n), b2.x, b2.y + 0.5);
    }}

    var label = t < MIX ? "공을 섞는 중…"
              : (spinning ? "추첨 진행 중…" : "추첨 완료");
    ctx.fillStyle = "rgba(255,255,255,0.75)";
    ctx.font = "600 15px system-ui, sans-serif";
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    ctx.fillText(label, 18, H - 16);

    if (t < done + 700) {{          // 마지막 공이 자리 잡은 뒤 조금 더 그린다
      rAF = requestAnimationFrame(frame);
    }}
  }}

  function start() {{
    if (rAF) cancelAnimationFrame(rAF);
    measure();
    setup();
    rAF = requestAnimationFrame(frame);
  }}

  // 레이아웃이 아직 잡히지 않았으면 다음 프레임에 다시 시도
  requestAnimationFrame(function () {{
    if (!canvas.clientWidth) requestAnimationFrame(start);
    else start();
  }});
}})();
</script>
"""


def render_html(html_block: str, fallback_height: int = 470) -> None:
    """JavaScript가 포함된 HTML 조각을 화면에 넣는다.

    최신 Streamlit은 st.html(unsafe_allow_javascript=True)로 스크립트를 실행하고,
    그 옵션이 없는 구버전에서는 iframe 컴포넌트로 대체한다.
    """
    try:
        st.html(html_block, unsafe_allow_javascript=True)
    except TypeError:
        import streamlit.components.v1 as components
        components.html(html_block, height=fallback_height, scrolling=False)


# ------------------------------------------------------------------------ UI ----
st.set_page_config(page_title="로또 6/45 분석·조합 스튜디오", page_icon="🎱", layout="wide")

st.markdown(
    """
<style>
  .block-container {padding-top: 2.2rem;}
  .hero {padding: 18px 22px;border-radius:16px;
         background:linear-gradient(120deg,#101a33 0%,#1d2a4d 55%,#243b6b 100%);
         color:#eef3ff;margin-bottom:14px}
  .hero h1 {margin:0;font-size:1.55rem}
  .hero p {margin:6px 0 0;font-size:.9rem;color:#b9c8e6}
  .notice {padding:10px 14px;border-radius:10px;font-size:.9rem;margin:6px 0}
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    f"""
<div class="hero">
  <h1>🎱 로또 6/45 분석·조합 스튜디오</h1>
  <p>동행복권 역대 당첨번호 통계 · 조건 필터 조합 · 비너스 추첨기 · 매일 추천 기록</p>
  <p>⚠️ 모든 조합의 1등 확률은 <b>1 / {TOTAL_COMBOS:,}</b> 로 완전히 같습니다.
     이 앱의 통계·점수는 <b>참고용 지표</b>이며 당첨을 보장하지 않습니다.
     구매는 여유 자금 범위에서, 재미로만 즐겨 주세요.</p>
</div>
""",
    unsafe_allow_html=True,
)

# --- 데이터 준비 ---
draws, source = load_draws()

if "auto_sync_msg" not in st.session_state:
    draws, sync_msg = auto_sync(draws)
    st.session_state["auto_sync_msg"] = sync_msg
auto_msg = st.session_state.get("auto_sync_msg")

if "notice" in st.session_state:
    level, msg = st.session_state.pop("notice")
    {"success": st.success, "info": st.info, "warning": st.warning,
     "error": st.error}.get(level, st.info)(msg)

# --- 사이드바 ---
with st.sidebar:
    st.subheader("📡 데이터 현황")
    if draws:
        latest = draws[-1]
        st.metric("최신 회차", f"{latest['drwNo']}회", help="동행복권 발표 기준 최신 회차")
        st.caption(f"추첨일 {latest.get('date') or '정보 없음'} · 전체 {len(draws):,}회차")
        st.write("최신 당첨번호")
        st.markdown(balls_html(latest["numbers"], 30) +
                    f'<div style="font-size:.78rem;color:#888;margin-left:4px">보너스 {latest.get("bonus")}</div>',
                    unsafe_allow_html=True)
    else:
        st.error("데이터가 없습니다.")

    st.divider()
    if st.button("🔄 최신 회차 업데이트", type="primary", width="stretch",
                 disabled=not draws, help="동행복권에서 새 회차만 받아옵니다"):
        with st.spinner("동행복권 서버에 확인 중…"):
            draws, level, msg = update_draws(draws)
        st.session_state["notice"] = (level, msg)
        st.rerun()
    if st.button("📥 전체 데이터 다시 받기", width="stretch",
                 help="1회차부터 최신까지 전부 다시 받아 캐시를 새로 만듭니다"):
        with st.spinner("전체 회차를 받아오는 중…"):
            draws, level, msg = refresh_all(draws)
        st.session_state["notice"] = (level, msg)
        st.rerun()

    st.divider()
    st.caption("데이터 출처: 동행복권(dhlottery.co.kr) 회차별 결과 조회")
    with st.expander("저장 위치 · 수집 방식"):
        st.caption(
            f"- 캐시: `{DRAWS_PATH}`\n"
            f"- 내장 기본 데이터: `seed_draws.json` ({seed_count()}회차)\n"
            f"- 추천 기록: `{HISTORY_PATH}`\n\n"
            "앱은 캐시 파일을 먼저 쓰고, 없으면 내장 데이터를 복사해 시작합니다. "
            "업데이트는 최신 회차 1건만, 전체 재수집은 한 번의 요청으로 받아옵니다. "
            "서버가 응답하지 않으면 캐시로 계속 동작하고 안내 문구를 표시합니다."
        )
    st.caption({"cache": "현재 데이터: 로컬 캐시", "seed": "현재 데이터: 내장 데이터",
                "network": "현재 데이터: 방금 내려받음", "empty": "현재 데이터: 없음"}.get(source, source))
    st.divider()
    st.caption(f"🗂️ 추천 기록 저장소: {storage_label()}")
    if storage_mode() == "gsheet":
        st.caption("추천 기록은 구글 시트('로또 분석기 추천 기록')에 저장됩니다.")
        if st.button("🔄 시트에서 기록 새로고침", width="stretch", key="side_refresh",
                     help="구글 시트에 저장된 최신 기록을 다시 읽어옵니다"):
            load_history(force=True)
            st.rerun()
    if auto_msg == "auto_failed":
        st.caption("최신 회차 자동 확인에 실패해 캐시 데이터로 표시 중입니다. "
                   "위 업데이트 버튼으로 다시 시도할 수 있습니다.")
    elif auto_msg:
        st.caption("🔄 " + auto_msg)

if not draws:
    st.error("당첨번호 데이터를 확보하지 못했습니다. 왼쪽의 '전체 데이터 다시 받기'를 눌러 주세요. "
             "계속 실패하면 `data/seed_draws.json` 파일이 있는지 확인해 주세요.")
    st.stop()

df = build_frame(draws)
profile = build_profile(df)
history = load_history()
saved_sets = history_sets(history)
if st.session_state.get(HISTORY_ERROR_KEY):
    st.warning(
        f"추천 기록 저장소({storage_label()}) 연결에 문제가 있습니다. "
        f"{st.session_state[HISTORY_ERROR_KEY]} — 새 기록 저장·삭제가 되지 않을 수 있습니다. "
        "사이드바의 '시트에서 기록 새로고침'으로 다시 시도해 주세요."
    )

latest = draws[-1]
try:
    latest_date = date.fromisoformat(latest.get("date") or "")
except ValueError:
    latest_date = None
target_round = latest["drwNo"] + 1
target_date = (latest_date + timedelta(days=7)) if latest_date else None
round_stale = bool(target_date and target_date < now_kst().date())

tab_stats, tab_maker, tab_draw, tab_log = st.tabs(
    ["📊 역대 통계", "🎯 조건 조합기", "🎱 추첨기", "📅 추천 기록"]
)

# ================================================== 1. 통계 대시보드 ==========
with tab_stats:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("분석 회차", f"{len(df):,}회")
    c2.metric("평균 총합", f"{df['합계'].mean():.1f}",
              help=f"최소 {df['합계'].min()} · 최대 {df['합계'].max()}")
    c3.metric("가장 많이 나온 홀짝", f"{int(df['홀수개수'].mode()[0])} : {6 - int(df['홀수개수'].mode()[0])}")
    c4.metric("최빈 저:고 개수 (1~22 : 23~45)",
              f"{int(df['저번호개수'].mode()[0])} : {6 - int(df['저번호개수'].mode()[0])}")

    st.markdown("#### 1~45 번호별 출현 빈도")
    incl_bonus = st.toggle("보너스 번호 포함", value=False, key="freq_bonus")
    freq = number_frequency(df, include_bonus=incl_bonus).reset_index()
    freq.columns = ["번호", "출현횟수"]
    base = alt.Chart(freq).encode(
        x=alt.X("번호:O", title="번호", axis=alt.Axis(labelAngle=0, labelFontSize=11)),
        y=alt.Y("출현횟수:Q", title="출현 횟수"),
    )
    bars = base.mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        color=alt.Color("출현횟수:Q", scale=alt.Scale(scheme="blues"), legend=None),
        tooltip=["번호", "출현횟수"],
    )
    rule = alt.Chart(pd.DataFrame({"y": [freq["출현횟수"].mean()]})).mark_rule(
        color="#e45756", strokeDash=[5, 4]).encode(y="y:Q")
    st.altair_chart((bars + rule).properties(height=300, title=f"평균 {freq['출현횟수'].mean():.1f}회 (빨간 점선)"),
                    width="stretch")

    st.markdown("#### 🔥 핫 · ❄️ 콜드 번호")
    window = st.slider("최근 N회 기준", 10, min(300, len(df)), 50, step=10, key="hotcold_window")
    recent = recent_window(df, window)
    col_hot, col_cold = st.columns(2)
    with col_hot:
        hot = recent.sort_values(["최근출현", "경과회차"], ascending=[False, True]).head(10)
        st.markdown(f"**최근 {window}회 다출현**")
        st.dataframe(hot.reset_index(drop=True), hide_index=True, height=390, width="stretch",
                     column_config={"번호": st.column_config.NumberColumn(format="%d"),
                                    "최근출현": st.column_config.ProgressColumn(
                                        "출현", format="%d회", min_value=0,
                                        max_value=int(max(recent['최근출현'].max(), 1)))})
    with col_cold:
        cold = recent.sort_values(["최근출현", "경과회차"], ascending=[True, False]).head(10)
        st.markdown(f"**최근 {window}회 미출현·소출현**")
        st.dataframe(cold.reset_index(drop=True), hide_index=True, height=390, width="stretch",
                     column_config={"번호": st.column_config.NumberColumn(format="%d"),
                                    "경과회차": st.column_config.NumberColumn(format="%d회 전")})

    st.markdown("#### 분포 분석")
    dist = distribution_table(df)
    d1, d2 = st.columns(2)
    with d1:
        st.markdown("**홀짝 비율 분포**")
        st.altair_chart(
            alt.Chart(dist["odd"]).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
                x=alt.X("홀수개수:O", title="홀수 개수"),
                y=alt.Y("비율:Q", title="비율(%)"),
                color=alt.Color("홀수개수:O", scale=alt.Scale(scheme="tealblues", reverse=True), legend=None),
                tooltip=["홀수개수", "회차수", "비율"],
            ).properties(height=260), width="stretch")
    with d2:
        st.markdown("**고저 비율 분포** (저 1~22 / 고 23~45)")
        st.altair_chart(
            alt.Chart(dist["low"]).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
                x=alt.X("저번호개수:O", title="저번호(1~22) 개수"),
                y=alt.Y("비율:Q", title="비율(%)"),
                color=alt.Color("저번호개수:O", scale=alt.Scale(scheme="purples", reverse=True), legend=None),
                tooltip=["저번호개수", "회차수", "비율"],
            ).properties(height=260), width="stretch")

    st.markdown("**6개 번호 총합 분포**")
    sum_df = dist["sum"]
    st.altair_chart(
        alt.Chart(sum_df).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("합계구간:O", title="총합 구간", axis=alt.Axis(labelAngle=-45, labelFontSize=10)),
            y=alt.Y("회차수:Q", title="회차 수"),
            color=alt.Color("회차수:Q", scale=alt.Scale(scheme="orangered"), legend=None),
            tooltip=["합계구간", "회차수", "비율"],
        ).properties(height=280), width="stretch")
    st.caption(
        f"역대 총합 평균 {df['합계'].mean():.1f} · 중앙값 {df['합계'].median():.0f} · "
        f"가장 흔한 구간 {sum_df.sort_values('회차수', ascending=False).iloc[0]['합계구간']}. "
        "총합 100~175 구간이 전체의 "
        f"{(df['합계'].between(100, 175).mean() * 100):.1f}%를 차지합니다."
    )

    st.markdown("#### 최근 5회 당첨번호")
    recent_rows = draws[-5:][::-1]
    for r in recent_rows:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #eee">'
            f'<div style="min-width:150px;font-size:.86rem;color:#555">{r["drwNo"]}회 · {r.get("date") or "-"}</div>'
            f'{balls_html(r["numbers"], 30)}'
            f'<div style="font-size:.8rem;color:#888">보너스 {r.get("bonus")}</div></div>',
            unsafe_allow_html=True)

# ================================================== 2. 조건 조합기 ============
with tab_maker:
    st.markdown("#### 조합 조건 설정")
    st.caption(f"대상 회차: **{target_round}회**"
               + (f" · 추첨일 {target_date}" if target_date else "")
               + (f" · ⚠️ 최신 데이터가 {latest['drwNo']}회차라 추첨일이 지났습니다. 사이드바에서 업데이트해 주세요."
                  if round_stale else ""))

    with st.container(border=True):
        fc1, fc2 = st.columns(2)
        fixed = fc1.multiselect("고정수 (반드시 포함)", NUMBERS, default=[],
                                format_func=lambda n: f"{n:02d}", max_selections=5,
                                help="최대 5개")
        excluded = fc2.multiselect("제외수 (절대 포함하지 않음)", NUMBERS, default=[],
                                   format_func=lambda n: f"{n:02d}")

        sc1, sc2, sc3 = st.columns([1, 1, 1.2])
        odd_range = sc1.slider("홀수 개수", 0, 6, (2, 4), key="odd_range")
        low_range = sc2.slider("저번호(1~22) 개수", 0, 6, (2, 4), key="low_range")
        sum_range = sc3.slider("총합 구간", 21, 255, (100, 175), step=5, key="sum_range")

        oc1, oc2, oc3 = st.columns([1, 1, 1.2])
        max_consec = oc1.slider("연속번호 최대 허용", 1, 6, 2, key="max_consec",
                                help="1 = 연속번호 금지, 2 = 12·13 같은 2연속까지 허용")
        count = oc2.number_input("생성할 조합 수", 1, 20, 5, step=1, key="combo_count")
        exclude_past = oc3.checkbox("역대 1등 당첨번호와 6개 완전 중복 제외", value=True,
                                    help="역대 1등 조합과 똑같은 6개는 만들지 않습니다")
        exclude_saved = st.checkbox("기존 추천 조합과 중복 제외 (오늘 포함)", value=True,
                                    help=f"지금까지 저장한 추천 {len(history)}건과 겹치지 않게 합니다")

    if st.button("✨ 새 조합 생성", type="primary", key="gen_btn"):
        if set(fixed) & set(excluded):
            st.error("같은 번호를 고정수와 제외수에 함께 넣을 수 없습니다.")
        else:
            try:
                past_wins = {tuple(sorted(d["numbers"])) for d in draws}
                combos, attempts, fails = generate_combos(
                    count=int(count), fixed=fixed, excluded=excluded,
                    odd_range=odd_range, low_range=low_range, sum_range=sum_range,
                    max_consec=int(max_consec), past_wins=past_wins,
                    saved_sets=saved_sets, profile=profile,
                    exclude_past=exclude_past, exclude_saved=exclude_saved,
                )
            except ValueError as exc:
                st.error(str(exc))
                combos, attempts, fails = [], 0, Counter()

            if combos:
                scored = [(c, score_combo(c, profile)) for c in combos]
                created = now_kst().isoformat(timespec="seconds")
                new_records = []
                for c, sc in scored:
                    new_records.append({
                        "id": f"{int(now_kst().timestamp() * 1000)}-{random.randint(100, 999)}",
                        "created_at": created,
                        "target_round": target_round,
                        "draw_date": target_date.isoformat() if target_date else None,
                        "numbers": c,
                        "mode": "조건 조합기",
                        "score": {k: sc[k] for k in ("적합도", "생일회피", "합계점수",
                                                     "홀짝점수", "고저점수", "합계", "홀수", "저번호", "연속")},
                        "filters": {"고정수": fixed, "제외수": excluded,
                                    "홀수개수": list(odd_range), "저번호개수": list(low_range),
                                    "총합구간": list(sum_range), "연속최대": int(max_consec)},
                    })
                if save_history(history + new_records):
                    history = load_history()
                    saved_sets = history_sets(history)
                    st.success(f"{len(combos)}개 조합을 생성하고 추천 기록에 저장했습니다. "
                               f"(시도 {attempts:,}회)")
                else:
                    st.warning("조합은 생성했지만 " + save_error_text() + ".")
                st.session_state["generated"] = [
                    {"numbers": c, "score": sc} for c, sc in scored]
            elif attempts:
                reason = ", ".join(f"{k} {v:,}회" for k, v in fails.most_common(3)) or "조건 불일치"
                st.warning(
                    f"{attempts:,}회 시도했지만 조건을 모두 만족하는 새 조합을 찾지 못했습니다. "
                    f"(탈락 사유: {reason}) 조건을 조금 넓혀 보세요 — 예: 총합 구간 확대, "
                    "연속번호 허용 3, 고정수/제외수 줄이기."
                )

    generated = st.session_state.get("generated") or []
    if generated:
        st.markdown("#### 생성된 조합")
        rows = []
        detail = []
        for i, g in enumerate(generated, 1):
            c, sc = g["numbers"], g["score"]
            rows.append({
                "조합": i,
                "번호": " · ".join(f"{n:02d}" for n in c),
                "홀:짝": f"{sc['홀수']}:{6 - sc['홀수']}",
                "고:저": f"{6 - sc['저번호']}:{sc['저번호']}",
                "합계": sc["합계"],
                "연속": sc["연속"],
                "분포 적합도": sc["적합도"],
                "생일 회피 지수": sc["생일회피"],
            })
            detail.append({
                "조합": i,
                "총합 점수": sc["합계점수"],
                "홀짝 점수": sc["홀짝점수"],
                "고저 점수": sc["고저점수"],
                "적합도": sc["적합도"],
                "생일 회피": sc["생일회피"],
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={
                         "분포 적합도": st.column_config.ProgressColumn(
                             "분포 적합도", format="%.1f", min_value=0, max_value=100),
                         "생일 회피 지수": st.column_config.ProgressColumn(
                             "생일 회피 지수", format="%d", min_value=0, max_value=100),
                     })
        with st.expander("점수 세부 내역 · 계산 방식"):
            st.dataframe(pd.DataFrame(detail), hide_index=True, width="stretch")
            st.caption(
                "**분포 적합도(0~100)** — 역대 당첨번호의 분포와 얼마나 닮았는지입니다. "
                "총합은 ±12 구간 밀도(가중 34%), 홀짝 개수(33%), 고저 개수(33%)의 상대 점수를 합산합니다. "
                "**생일 회피 지수(0~100)** — 32~45 번호의 비중입니다. 생일(1~31)로 번호를 고르는 사람이 많아, "
                "높은 번호가 많을수록 당첨 시 동점자가 줄어드는 경향이 있습니다. "
                f"역대 당첨번호에서 32~45가 차지한 비중은 {profile['high_ratio'] * 100:.1f}%입니다."
            )
        st.caption(f"오늘 저장된 추천: {sum(1 for r in history if str(r.get('created_at', '')).startswith(now_kst().date().isoformat()))}건 "
                   f"· 전체 {len(history)}건")

    st.info(
        f"모든 조합의 1등 확률은 **1/{TOTAL_COMBOS:,}** 로 동일합니다. "
        "적합도·생일 회피 지수는 역대 분포와 동점자 경향을 보여 주는 참고 지표일 뿐, "
        "당첨 확률을 높이지 않습니다."
    )

# ================================================== 3. 추첨기 ================
with tab_draw:
    st.markdown("#### 비너스 추첨기 방식 추출")
    st.caption("45개 공이 통 안에서 섞인 뒤 하나씩 순서대로 나옵니다. "
               "번호는 파이썬 난수(Fisher–Yates 셔플)로 먼저 정해지고, 화면은 그 결과를 연출합니다.")

    dc1, dc2 = st.columns([2, 1])
    with dc1:
        ven_excluded = st.multiselect("제외수 (선택)", NUMBERS, default=[],
                                      format_func=lambda n: f"{n:02d}", key="ven_excl")
        ven_dedup = st.checkbox("기존 추천 조합과 중복 회피", value=True, key="ven_dedup")
    with dc2:
        st.write("")
        st.write("")
        go = st.button("🎬 추첨 시작", type="primary", key="ven_btn", width="stretch")
        anim = st.checkbox("공이 하나씩 나오는 애니메이션", value=True, key="ven_anim",
                           help="캔버스 그래픽 대신 번호가 순서대로 나타나는 애니메이션도 함께 보여 줍니다")

    def draw_numbers(excluded: list[int], avoid: set, dedup: bool) -> list[int]:
        rng = random.SystemRandom()
        pool = [n for n in NUMBERS if n not in set(excluded)]
        if len(pool) < 6:
            raise ValueError("제외수가 너무 많습니다. 6개 이상 남겨 주세요.")
        for _ in range(4000):
            order = rng.sample(pool, 6)
            if not dedup or tuple(sorted(order)) not in avoid:
                return order
        return order

    if go or "venus_order" not in st.session_state:
        try:
            st.session_state["venus_order"] = draw_numbers(ven_excluded, saved_sets, ven_dedup)
            st.session_state["venus_at"] = now_kst().isoformat(timespec="seconds")
            st.session_state["venus_animate"] = bool(anim) and go
        except ValueError as exc:
            st.error(str(exc))

    order = st.session_state.get("venus_order") or []
    if order:
        render_html(venus_html(order))
        if st.session_state.pop("venus_animate", False):
            reveal = st.empty()
            for i in range(1, 7):
                reveal.markdown(
                    balls_html(order[:i], 40)
                    + f'<div style="font-size:.82rem;color:#888">{i} / 6</div>',
                    unsafe_allow_html=True)
                time.sleep(0.45)
        nums = sorted(order)
        m1, m2, m3 = st.columns([2, 1, 1])
        with m1:
            st.markdown("**추첨 결과**")
            st.markdown(balls_html(order, 40), unsafe_allow_html=True)
            st.caption("↑ 나온 순서 · " + " · ".join(f"{n:02d}" for n in nums) + " (정렬)")
        sc = score_combo(nums, profile)
        m2.metric("분포 적합도", f"{sc['적합도']:.1f}", help="0~100, 역대 분포 유사도")
        m3.metric("생일 회피 지수", f"{sc['생일회피']}", help="32~45 번호 비중(0~100)")

        if st.button("💾 이 결과를 추천 기록에 저장", key="ven_save"):
            rec = {
                "id": f"{int(now_kst().timestamp() * 1000)}-ven",
                "created_at": now_kst().isoformat(timespec="seconds"),
                "target_round": target_round,
                "draw_date": target_date.isoformat() if target_date else None,
                "numbers": nums,
                "mode": "비너스 추첨기",
                "score": {k: sc[k] for k in ("적합도", "생일회피", "합계점수",
                                             "홀짝점수", "고저점수", "합계", "홀수", "저번호", "연속")},
                "filters": {"제외수": ven_excluded, "중복회피": bool(ven_dedup)},
            }
            if save_history(history + [rec]):
                st.session_state["notice"] = (
                    "success", f"{target_round}회 대상 조합을 추천 기록에 저장했습니다.")
                st.rerun()
            else:
                st.warning(save_error_text() + ".")
        st.caption("애니메이션이 보이지 않는 환경이면 위의 번호가 실제 결과입니다.")
    else:
        st.info("'추첨 시작'을 눌러 주세요.")

    st.warning(f"추첨기 결과도 1등 확률은 1/{TOTAL_COMBOS:,}입니다. 연출은 결과에 영향을 주지 않습니다.")

# ================================================== 4. 추천 기록 ==============
with tab_log:
    st.markdown("#### 추천 기록")
    today_str = now_kst().date().isoformat()
    today_cnt = sum(1 for r in history if str(r.get("created_at", "")).startswith(today_str))
    lc1, lc2, lc3 = st.columns(3)
    lc1.metric("오늘 생성", f"{today_cnt}건")
    lc2.metric("전체 기록", f"{len(history)}건")
    lc3.metric("대상 회차", f"{target_round}회" + (f" ({target_date})" if target_date else ""))

    if not history:
        st.info("아직 저장된 추천이 없습니다. '조건 조합기'에서 조합을 만들면 자동으로 기록됩니다.")
    else:
        hdf = history_rows(history)
        modes = sorted({str(r.get("mode") or "-") for r in history})
        f1, f2 = st.columns([1, 1])
        mode_filter = f1.multiselect("모드", modes, default=modes, key="log_mode")
        only_today = f2.checkbox("오늘 것만 보기", value=False, key="log_today")

        view = hdf.copy()
        view = view[view["모드"].isin(mode_filter)]
        if only_today:
            view = view[view["생성시각"].astype(str).str.startswith(today_str)]
        st.dataframe(view.iloc[::-1], hide_index=True, width="stretch", height=380)

        csv_bytes = hdf.to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ 전체 기록 CSV 다운로드", data=csv_bytes,
                           file_name=f"lotto_recommendations_{today_str}.csv",
                           mime="text/csv", key="log_csv")

        with st.expander("기록 삭제"):
            labels = [f"{i + 1}. {r.get('created_at')} · {' '.join(f'{n:02d}' for n in r['numbers'])}"
                      for i, r in enumerate(history)]
            pick = st.selectbox("삭제할 기록", range(len(history)), format_func=lambda i: labels[i],
                                key="log_del_pick")
            confirm = st.checkbox("정말 삭제합니다 (되돌릴 수 없음)", key="log_del_confirm")
            if st.button("선택 기록 삭제", disabled=not confirm, key="log_del_btn"):
                removed = history[pick]
                remaining = [r for i, r in enumerate(history) if i != pick]
                if save_history(remaining):
                    st.success(f"삭제했습니다: {' '.join(f'{n:02d}' for n in removed['numbers'])}")
                    st.rerun()
                else:
                    st.error(save_error_text() + ".")

    st.caption(
        f"기록 저장소: {storage_label()} · {storage_target()} · 생성일·대상 회차별로 저장되며, "
        "새 조합은 기존 기록과 겹치지 않게 만들어집니다."
    )
