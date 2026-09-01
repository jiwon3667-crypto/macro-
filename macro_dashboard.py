"""
매크로 대시보드 — 7범주 12지표를 '현재값 + 방향'만 표시.

점수화·종합판정 없음. 각 지표를 억지로 합쳐 LONG/SHORT를 뽑지 않는다.
그냥 "각 범주가 지금 어떤 상태·어느 방향인가"를 보여주는 계기판.

데이터는 전부 FRED (무료). yfinance 불필요 → 표준 라이브러리만 사용.

범주 / 지표:
  성장      : 실질GDP(YoY), 소매판매(YoY), 산업생산(YoY)   ← PMI는 ISM 유료라 산업생산으로 대용
  물가      : CPI(YoY), 근원 PCE(YoY)
  고용      : 비농업고용(월간 증감), 신규 실업수당청구
  금리·정책 : 미 10년물, 10Y-2Y 스프레드
  유동성·신용: 하이일드 스프레드
  환율      : 광의 달러지수
  심리      : VIX
"""

import os
import sys
import json
import datetime as dt
import urllib.request
import urllib.parse

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


# --- 지표 정의 -------------------------------------------------------------
# mode:
#   "yoy"      : 전년동기 대비 %. 방향 = 그 YoY가 가속(↑)/감속(↓) — 직전 기간 YoY와 비교
#   "level"    : 값 그대로. 방향 = 약 1개월 전 대비 상승/하락
#   "mom_diff" : 전월 대비 증감(레벨 차이, 예: 고용 증가 인원). 방향 = 증감폭이 커졌나 작아졌나
#   "wow_level": 주간 값. 방향 = 약 4주 전 대비
INDICATORS = [
    # (범주, 라벨, FRED시리즈, mode, 단위, 소수자리, 표시스케일)
    #   표시스케일: 표시할 때 val 에 곱함. ICSA는 실제 건수라 /1000 해서 천건으로.
    ("성장",        "실질GDP",       "GDPC1",         "yoy",      "% YoY", 1,    1),
    ("성장",        "소매판매",       "RSAFS",         "yoy",      "% YoY", 1,    1),
    ("성장",        "산업생산",       "INDPRO",        "yoy",      "% YoY", 1,    1),
    ("물가",        "CPI",           "CPIAUCSL",      "yoy",      "% YoY", 1,    1),
    ("물가",        "근원PCE",        "PCEPILFE",      "yoy",      "% YoY", 1,    1),
    ("고용",        "비농업고용",      "PAYEMS",        "mom_diff", "천명",  0,    1),
    ("고용",        "신규실업청구",    "ICSA",          "wow_level","천건",  0,    0.001),
    ("금리·정책",   "미10년물",       "DGS10",         "level",    "%",     2,    1),
    ("금리·정책",   "10Y-2Y",        "T10Y2Y",        "level",    "%p",    2,    1),
    ("유동성·신용", "HY스프레드",      "BAMLH0A0HYM2",  "level",    "%p",    2,    1),
    ("환율",        "달러지수(광의)",  "DTWEXBGS",      "level",    "",      2,    1),
    ("심리",        "VIX",           "VIXCLS",        "level",    "",      1,    1),
]


def fred(series_id, limit=400):
    if not FRED_API_KEY:
        sys.exit("환경변수 FRED_API_KEY가 없습니다.")
    q = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    })
    with urllib.request.urlopen(f"{FRED_URL}?{q}", timeout=30) as r:
        data = json.load(r)
    out = []
    for o in data.get("observations", []):
        if o["value"] not in (".", "", None):
            out.append((o["date"], float(o["value"])))
    return out  # 최신순 (index 0 = 최근)


def arrow(cur, base):
    if cur > base:
        return "↑"
    if cur < base:
        return "↓"
    return "→"


def compute(series, mode):
    """
    반환: (표시값, 방향화살표, 보조텍스트)
    데이터 부족·에러 시 예외 없이 (None, "?", "데이터없음") 반환.
    """
    try:
        obs = fred(series)
        if not obs:
            return None, "?", "데이터없음"

        if mode == "yoy":
            idx = {d: v for d, v in obs}
            dates = [d for d, _ in obs]

            def yoy(i):
                d = dates[i]
                y, m = int(d[:4]), int(d[5:7])
                cand = [k for k in idx if k[:7] == f"{y-1:04d}-{m:02d}"]
                if not cand:
                    return None
                return (idx[d] / idx[cand[0]] - 1) * 100

            cur = yoy(0)
            prev = yoy(1)
            if cur is None:
                return None, "?", "YoY계산불가"
            a = arrow(cur, prev) if prev is not None else "→"
            sub = f"직전 {prev:.1f}" if prev is not None else ""
            return cur, a, sub

        if mode == "level":
            cur = obs[0][1]
            # 약 1개월 전: 일간이면 ~21거래일, 월간이면 1개
            base = obs[21][1] if len(obs) > 21 else obs[min(1, len(obs)-1)][1]
            return cur, arrow(cur, base), f"1M전 {base:.2f}"

        if mode == "wow_level":
            cur = obs[0][1]
            base = obs[4][1] if len(obs) > 4 else obs[min(1, len(obs)-1)][1]
            return cur, arrow(cur, base), f"4주전 {base:.0f}"

        if mode == "mom_diff":
            # 최신 - 직전 = 이번 달 증감. 방향 = 증감폭이 지난달보다 커졌나
            cur_diff = obs[0][1] - obs[1][1]
            prev_diff = obs[1][1] - obs[2][1]
            return cur_diff, arrow(cur_diff, prev_diff), f"전월증감 {prev_diff:+.0f}"

    except Exception as e:
        return None, "?", f"오류:{type(e).__name__}"

    return None, "?", ""


def build_message():
    today = dt.date.today().isoformat()
    lines = [f"📊 매크로 대시보드 ({today})", ""]
    last_cat = None
    for cat, label, series, mode, unit, digits, scale in INDICATORS:
        if cat != last_cat:
            if last_cat is not None:
                lines.append("")          # 범주 사이 빈 줄
            lines.append(f"【{cat}】")
            last_cat = cat
        val, a, sub = compute(series, mode)
        if val is None:
            lines.append(f"  {label}: — {a} ({sub})")
        else:
            fmt = f"{{:+.{digits}f}}" if mode == "mom_diff" else f"{{:.{digits}f}}"
            vtxt = fmt.format(val * scale)
            unit_txt = (" " + unit).rstrip()
            sub_txt = f"  ({sub})" if sub else ""
            lines.append(f"  {label}: {vtxt}{unit_txt} {a}{sub_txt}")
    return "\n".join(lines).rstrip()


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat_id):
        print("[텔레그램 미설정 — 콘솔 출력만]\n")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    with urllib.request.urlopen(url, data=payload, timeout=30) as r:
        r.read()


if __name__ == "__main__":
    msg = build_message()
    print(msg)
    send_telegram(msg)
