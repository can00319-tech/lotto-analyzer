"""Streamlit AppTest 기반 스모크 테스트.

실행:  python3 test_app.py
임시 데이터 폴더에서 실제 앱 app.py를 실행해 주요 화면과 기능을 검증합니다.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from streamlit.testing.v1 import AppTest

HERE = Path(__file__).resolve().parent
APP = str(HERE / "app.py")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  → {detail}"))
    if not cond:
        failures.append(name)


def make_env() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="lotto-test-"))
    (tmp / "data").mkdir()
    shutil.copy(HERE / "data" / "seed_draws.json", tmp / "data" / "seed_draws.json")
    os.environ["LOTTO_DATA_DIR"] = str(tmp / "data")
    os.environ["LOTTO_NO_AUTOSYNC"] = "1"   # 테스트는 네트워크에 의존하지 않는다
    return tmp


def new_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    return at


def main() -> int:
    env = make_env()
    data_dir = env / "data"
    print("임시 데이터 폴더:", data_dir)

    # 1) 첫 실행 – 시드에서 캐시 생성
    print("\n[1] 초기 로드")
    at = new_app()
    check("예외 없음", not at.exception, str(at.exception))
    cache = data_dir / "draws.json"
    check("캐시 파일 생성", cache.exists())
    draws = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []
    check("회차 수 1,200 이상", len(draws) >= 1200, f"{len(draws)}회차")
    check("최신 회차 1,250", draws and draws[-1]["drwNo"] == 1250)
    labels = [m.label for m in at.metric]
    check("최신 회차 지표 표시", "최신 회차" in labels, str(labels))
    check("탭 4개", len(at.tabs) == 4, str([t.label for t in at.tabs] if at.tabs else []))

    # 2) 통계 탭
    print("\n[2] 통계 화면")
    check("차트 4개 이상", len(at.get("vega_lite_chart")) >= 4,
          str(len(at.get("vega_lite_chart"))))
    check("데이터프레임 2개 이상", len(at.dataframe) >= 2, str(len(at.dataframe)))
    hot_df = at.dataframe[0].value
    check("핫 번호 10행", len(hot_df) == 10, str(len(hot_df)))
    check("콜드 번호 10행", len(at.dataframe[1].value) == 10, str(len(at.dataframe[1].value)))

    # 슬라이더 변경 후 재실행
    at.slider(key="hotcold_window").set_value(100).run()
    check("최근 N회 변경 후 예외 없음", not at.exception, str(at.exception))

    # 3) 조건 조합기
    print("\n[3] 조합 생성")
    at.multiselect[0].set_value([7]).run() if at.multiselect else None
    check("고정수 선택 후 예외 없음", not at.exception, str(at.exception))
    at.slider(key="sum_range").set_value((110, 170)).run()
    at.button(key="gen_btn").click().run()
    check("생성 후 예외 없음", not at.exception, str(at.exception))
    hist_path = data_dir / "history.json"
    check("기록 파일 생성", hist_path.exists())
    hist = json.loads(hist_path.read_text(encoding="utf-8"))["records"] if hist_path.exists() else []
    check("5개 저장", len(hist) == 5, f"{len(hist)}건")
    if hist:
        rec = hist[0]
        nums = rec["numbers"]
        check("고정수 7 포함", 7 in nums, str(nums))
        check("홀수 2~4개", 2 <= sum(n % 2 for n in nums) <= 4, str(nums))
        check("저번호 2~4개", 2 <= sum(n <= 22 for n in nums) <= 4, str(nums))
        check("총합 110~170", 110 <= sum(nums) <= 170, str(sum(nums)))
        check("번호 6개·중복 없음", len(set(nums)) == 6, str(nums))
    check("성공 메시지", any("저장했습니다" in str(s.value) for s in at.success),
          str([s.value for s in at.success]))

    # 두 번째 생성 – 기존 기록과 중복 없이
    at.button(key="gen_btn").click().run()
    hist2 = json.loads(hist_path.read_text(encoding="utf-8"))["records"] if hist_path.exists() else []
    sets = [tuple(sorted(r["numbers"])) for r in hist2]
    check("10건 누적", len(hist2) == 10, f"{len(hist2)}건")
    check("기록 간 중복 없음", len(set(sets)) == len(sets), str(sets))

    # 4) 추첨기
    print("\n[4] 비너스 추첨기")
    at2 = new_app()
    if "venus_order" not in at2.session_state:
        check("자동 추첨 결과 존재", False, "session_state에 venus_order 없음")
    else:
        order = at2.session_state["venus_order"]
        check("추첨 번호 6개", len(order) == 6 and len(set(order)) == 6, str(order))
    at2.button(key="ven_btn").click().run()
    check("재추첨 예외 없음", not at2.exception, str(at2.exception))
    order2 = at2.session_state["venus_order"]
    check("재추첨 값 갱신", len(order2) == 6, str(order2))
    at2.button(key="ven_save").click().run()
    hist3 = json.loads(hist_path.read_text(encoding="utf-8"))["records"]
    check("추첨 결과 저장", any(r["mode"] == "비너스 추첨기" for r in hist3), str(len(hist3)))

    # 5) 기록 화면 / CSV
    print("\n[5] 기록 화면")
    check("다운로드 버튼 존재", len(at2.get("download_button")) >= 1,
          str(len(at2.get("download_button"))))
    csv_url = at2.get("download_button")[0].proto.url if at2.get("download_button") else ""
    check("CSV 다운로드 링크 생성", csv_url.endswith(".csv"), csv_url)
    check("기록 표 표시", any(len(d.value) >= 1 for d in at2.dataframe if hasattr(d.value, "__len__")))

    # 6) 기록 삭제
    at2.checkbox(key="log_del_confirm").check().run()
    before = len(json.loads(hist_path.read_text(encoding="utf-8"))["records"])
    at2.button(key="log_del_btn").click().run()
    after = len(json.loads(hist_path.read_text(encoding="utf-8"))["records"])
    check("기록 1건 삭제", after == before - 1, f"{before} → {after}")

    # 7) 손상된 캐시 복구
    print("\n[6] 예외 복구")
    cache.write_text("{ broken json", encoding="utf-8")
    at3 = new_app()
    check("손상 캐시에서도 실행", not at3.exception, str(at3.exception))
    check("시드로 자동 복구", len(at3.dataframe) >= 2)

    print("\n" + ("=" * 46))
    if failures:
        print(f"실패 {len(failures)}건: " + ", ".join(failures))
        return 1
    print("모든 스모크 테스트 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
