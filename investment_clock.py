"""
Investment Clock (Merrill Lynch, Greetham & Hartnett, 2004) — 순정 원문 구현.

원문이 보는 지표는 딱 2개:
  1) 성장축  : OECD Output Gap 방향 → 실무 대체로 OECD CLI 방향
  2) 인플레축: 헤드라인 CPI YoY 방향 (상승=가속 / 하락=감속)

두 축의 방향 부호를 조합해 4국면 중 하나로 분류한다.
가중치·z-score·정책 오버레이 없음. 원문 Table 1 그대로.

    Growth ↑ + Inflation ↓ → Recovery    → 주식
    Growth ↑ + Inflation ↑ → Overheat    → 원자재
    Growth ↓ + Inflation ↑ → Stagflation → 현금
    Growth ↓ + Inflation ↓ → Reflation   → 채권
"""

import os
import sys
import json
import urllib.request
import urllib.parse

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# --- FRED series IDs -------------------------------------------------------
# 성장축: OECD CLI (미국, amplitude-adjusted index, trend-restored).
#   방향(전월 대비 상승/하락)만 사용.
#   ※ OECD가 가끔 시리즈를 개편하므로 값이 안 나오면 FRED에서 최신 ID 확인.
CLI_SERIES = "USALOLITOTRSTSAM"       # CLI index (trend restored)

# 인플레축: 헤드라인 CPI (All Urban Consumers, index).
#   여기서 YoY를 계산하고, 그 YoY의 방향(가속/감속)을 사용.
CPI_SERIES = "CPIAUCSL"

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def fred(series_id, limit=20):
    """FRED에서 최신 관측치를 최신순으로 가져와 (date, value) 리스트로 반환."""
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
    if not out:
        sys.exit(f"{series_id}: 유효한 데이터가 없습니다. 시리즈 ID를 확인하세요.")
    return out  # 최신순 (index 0 = 가장 최근)


def cli_direction():
    """CLI 지수의 전월 대비 방향. 반환: (부호 +1/-1, 최신값, 직전값)."""
    obs = fred(CLI_SERIES, limit=6)
    latest, prev = obs[0][1], obs[1][1]
    return (1 if latest >= prev else -1), latest, prev


def cpi_yoy_direction():
    """
    헤드라인 CPI YoY의 방향(가속/감속).
    최신 YoY vs 직전 달 YoY 를 비교.
    반환: (부호 +1/-1, 최신 YoY%, 직전 YoY%).
    """
    obs = fred(CPI_SERIES, limit=15)          # 최신순, 14개월치 이상
    idx = {d: v for d, v in obs}
    dates = [d for d, _ in obs]               # 최신순

    def yoy(i):
        d = dates[i]
        y, m = int(d[:4]), int(d[5:7])
        prior_key = f"{y-1:04d}-{m:02d}-{d[8:]}"
        # 정확한 일자 매칭이 안 되면 같은 연-월로 근사 매칭
        cand = [k for k in idx if k[:7] == f"{y-1:04d}-{m:02d}"]
        if prior_key in idx:
            base = idx[prior_key]
        elif cand:
            base = idx[cand[0]]
        else:
            return None
        return (idx[d] / base - 1) * 100

    latest_yoy = yoy(0)
    prev_yoy = yoy(1)
    if latest_yoy is None or prev_yoy is None:
        sys.exit("CPI YoY 계산 실패: 12개월 전 데이터 부족.")
    return (1 if latest_yoy >= prev_yoy else -1), latest_yoy, prev_yoy


# --- 원문 Table 1 분류 ------------------------------------------------------
PHASES = {
    (1, -1): ("Recovery",    "주식 (Stocks)",       "Cyclical Growth"),
    (1,  1): ("Overheat",    "원자재 (Commodities)", "Cyclical Value"),
    (-1, 1): ("Stagflation", "현금 (Cash)",          "Defensive Value"),
    (-1, -1): ("Reflation",  "채권 (Bonds)",         "Defensive Growth"),
}


def classify():
    g_sign, cli_now, cli_prev = cli_direction()
    i_sign, cpi_now, cpi_prev = cpi_yoy_direction()
    phase, asset, sector = PHASES[(g_sign, i_sign)]

    g_arrow = "↑ 상승" if g_sign == 1 else "↓ 하락"
    i_arrow = "↑ 가속" if i_sign == 1 else "↓ 감속"

    msg = (
        f"🕐 Investment Clock (순정 원문)\n\n"
        f"성장축 · OECD CLI: {g_arrow}\n"
        f"   {cli_prev:.2f} → {cli_now:.2f}\n"
        f"인플레축 · CPI YoY: {i_arrow}\n"
        f"   {cpi_prev:.2f}% → {cpi_now:.2f}%\n\n"
        f"▶ 국면: {phase}\n"
        f"▶ 최적 자산: {asset}\n"
        f"▶ 최적 섹터: {sector}"
    )
    return msg


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
    out = classify()
    print(out)
    send_telegram(out)
