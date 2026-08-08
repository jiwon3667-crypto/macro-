#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매크로 종합 롱숏 시그널 생성기
- FRED + Yahoo Finance 데이터 수집
- 지표별 신호(-1~+1) 산출 → 가중합 → LONG/SHORT/NEUTRAL 판정
- 성장/인플 2축으로 매크로 국면(4분면) 태깅
- 텔레그램 발송
매일 새벽 GitHub Actions cron으로 실행.
"""

import os
import sys
import datetime as dt
import requests
import numpy as np
import pandas as pd

# ------------------------------------------------------------------
# 1. 지표 정의
#   src   : "fred" | "yf" | "ratio"
#   id    : 시리즈 코드 (ratio는 "분자/분모")
#   rdir  : 리스크 방향 (+1: 값↑=리스크온,  -1: 값↑=리스크오프)
#   w     : 리스크 종합점수 가중치 (내부에서 정규화)
#   method: "pct"(레벨 백분위) | "ma"(200일선 이격)
#   note  : (양수신호 설명, 음수신호 설명)
#   gdir  : 성장축 방향 (없으면 0)   +1=값↑→성장↑
#   idir  : 인플축 방향 (없으면 0)   +1=값↑→인플↑
# ------------------------------------------------------------------
INDICATORS = {
    # --- 크레딧 & 리스크 (가장 신뢰도 높은 리스크 게이지) ---
    "HY_OAS":    dict(src="fred", id="BAMLH0A0HYM2", rdir=-1, w=0.13, method="pct",
                      note=("하이일드 스프레드 축소", "하이일드 스프레드 확대"), gdir=-1, idir=0),
    "IG_OAS":    dict(src="fred", id="BAMLC0A0CM",   rdir=-1, w=0.06, method="pct",
                      note=("투자등급 스프레드 축소", "투자등급 스프레드 확대"), gdir=-1, idir=0),
    "VIX":       dict(src="yf",   id="^VIX",         rdir=-1, w=0.08, method="pct",
                      note=("주식 변동성 안정", "주식 변동성 급등"), gdir=0, idir=0),
    "MOVE":      dict(src="yf",   id="^MOVE",        rdir=-1, w=0.06, method="pct",
                      note=("채권 변동성 안정", "채권 변동성 급등"), gdir=0, idir=0),

    # --- 금융환경 & 유동성 ---
    "NFCI":      dict(src="fred", id="NFCI",         rdir=-1, w=0.12, method="pct",
                      note=("금융환경 완화적", "금융환경 긴축적"), gdir=-1, idir=0),
    "DXY":       dict(src="yf",   id="DX-Y.NYB",     rdir=-1, w=0.07, method="pct",
                      note=("달러 약세(위험선호)", "달러 강세(안전선호)"), gdir=0, idir=0),

    # --- 성장 ---
    "CLAIMS":    dict(src="fred", id="ICSA",         rdir=-1, w=0.07, method="pct",
                      note=("실업청구 감소(고용양호)", "실업청구 증가(고용둔화)"), gdir=-1, idir=0),
    "COPGOLD":   dict(src="ratio",id="HG=F/GC=F",    rdir=+1, w=0.08, method="pct",
                      note=("구리/금 상승(성장기대)", "구리/금 하락(방어선호)"), gdir=+1, idir=+1),
    "CURVE":     dict(src="fred", id="T10Y2Y",       rdir=+1, w=0.05, method="pct",
                      note=("장단기금리차 스티프닝", "장단기금리차 플래트닝"), gdir=+1, idir=0),

    # --- 인플레 (국면 판정용) ---
    "BREAKEVEN": dict(src="fred", id="T5YIFR",       rdir=0,  w=0.04, method="pct",
                      note=("기대인플레 상승", "기대인플레 하락"), gdir=0, idir=+1),
    "REAL10Y":   dict(src="fred", id="DFII10",       rdir=-1, w=0.07, method="pct",
                      note=("실질금리 하락(우호적)", "실질금리 상승(긴축압박)"), gdir=0, idir=0),

    # --- 가격 모멘텀 ---
    "SPX_200":   dict(src="yf",   id="^GSPC",        rdir=+1, w=0.10, method="ma",
                      note=("S&P 200일선 위(추세강세)", "S&P 200일선 아래(추세약세)"), gdir=+1, idir=0),
}

LONG_TH = 0.15    # 종합점수 > +0.15 → LONG
SHORT_TH = -0.15  # 종합점수 < -0.15 → SHORT
LOOKBACK = 252    # 백분위/모멘텀 산출 윈도우 (거래일 1년)


# ------------------------------------------------------------------
# 2. 데이터 수집
# ------------------------------------------------------------------
def fetch_fred(series_id, key, start="2021-01-01"):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = dict(series_id=series_id, api_key=key, file_type="json",
                  observation_start=start)
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    obs = r.json()["observations"]
    data = {o["date"]: float(o["value"]) for o in obs if o["value"] not in (".", "")}
    s = pd.Series(data)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def fetch_yf(ticker):
    import yfinance as yf
    df = yf.download(ticker, period="2y", interval="1d",
                     progress=False, auto_adjust=False)
    return df["Close"].dropna().squeeze()


def fetch_series(cfg, fred_key):
    if cfg["src"] == "fred":
        return fetch_fred(cfg["id"], fred_key)
    if cfg["src"] == "yf":
        return fetch_yf(cfg["id"])
    if cfg["src"] == "ratio":
        num, den = cfg["id"].split("/")
        a, b = fetch_yf(num), fetch_yf(den)
        df = pd.concat([a, b], axis=1).dropna()
        return (df.iloc[:, 0] / df.iloc[:, 1])
    raise ValueError(cfg["src"])


# ------------------------------------------------------------------
# 3. 지표별 신호 산출 (전부 "값이 높을수록 +1" 방향으로 정규화, rdir 미적용)
# ------------------------------------------------------------------
def raw_signal(series, method):
    s = series.dropna().astype(float)
    if len(s) < 60:
        return np.nan

    if method == "ma":
        ma = s.rolling(200).mean()
        level = float((s.iloc[-1] / ma.iloc[-1] - 1) / 0.08)  # ±8% 이격 → ±1
        level = float(np.clip(level, -1, 1))
    else:  # "pct" — 최근값의 1년 백분위
        win = s.iloc[-LOOKBACK:]
        pct = float(win.rank(pct=True).iloc[-1])
        level = 2 * pct - 1

    # 모멘텀 오버레이 (20일 변화의 z-score)
    chg = s.diff(20).dropna()
    ref = chg.iloc[-LOOKBACK:]
    z = (chg.iloc[-1] - ref.mean()) / (ref.std() + 1e-9)
    mom = float(np.clip(z, -1, 1))

    return float(np.clip(0.7 * level + 0.3 * mom, -1, 1))


# ------------------------------------------------------------------
# 4. 종합
# ------------------------------------------------------------------
def build_report(fred_key):
    signals, errors = {}, {}
    for name, cfg in INDICATORS.items():
        try:
            s = fetch_series(cfg, fred_key)
            signals[name] = raw_signal(s, cfg["method"])
        except Exception as e:  # 개별 실패는 스킵
            errors[name] = str(e)[:80]
            signals[name] = np.nan

    # 리스크 종합점수 (rdir 적용, 결측 제외 후 가중 정규화)
    num = den = 0.0
    contrib = {}
    for name, cfg in INDICATORS.items():
        sig, rdir, w = signals[name], cfg["rdir"], cfg["w"]
        if np.isnan(sig) or rdir == 0:
            continue
        c = rdir * sig
        contrib[name] = c
        num += w * c
        den += w
    score = num / den if den else 0.0

    # 성장축 / 인플축
    def axis(key):
        vals = [cfg[key] * signals[n] for n, cfg in INDICATORS.items()
                if cfg[key] != 0 and not np.isnan(signals[n])]
        return float(np.mean(vals)) if vals else 0.0
    g, i = axis("gdir"), axis("idir")

    # 방향 판정
    if score > LONG_TH:
        direction = "🟢 LONG (리스크온)"
    elif score < SHORT_TH:
        direction = "🔴 SHORT (리스크오프)"
    else:
        direction = "⚪ NEUTRAL (관망)"

    # 국면 4분면
    regime = {
        (True, True):   "리플레이션 (성장↑ 인플↑) — 경기민감·원자재 우위",
        (True, False):  "골디락스 (성장↑ 인플↓) — 리스크자산 우호",
        (False, True):  "스태그플레이션 (성장↓ 인플↑) — 방어·현금",
        (False, False): "디플레 (성장↓ 인플↓) — 채권·안전선호",
    }[(g >= 0, i >= 0)]

    return dict(score=score, direction=direction, g=g, i=i,
                regime=regime, signals=signals, contrib=contrib, errors=errors)


# ------------------------------------------------------------------
# 5. 텔레그램 메시지
# ------------------------------------------------------------------
def format_message(rep):
    now = dt.datetime.utcnow() + dt.timedelta(hours=9)
    lines = [
        f"🌅 *매크로 롱숏 시그널*  ({now:%Y-%m-%d %H:%M} KST)",
        "",
        f"📊 종합점수: *{rep['score']:+.2f}*  →  {rep['direction']}",
        f"🧭 국면: {rep['regime']}",
        f"     성장축 {rep['g']:+.2f} | 인플축 {rep['i']:+.2f}",
        "",
        "*주요 지표* (기여도순):",
    ]
    items = sorted(rep["contrib"].items(), key=lambda x: -abs(x[1]))
    for name, c in items:
        cfg = INDICATORS[name]
        emoji = "🟢" if c > 0.05 else ("🔴" if c < -0.05 else "⚪")
        desc = cfg["note"][0] if c >= 0 else cfg["note"][1]
        lines.append(f"  {emoji} `{name:<9}` {c:+.2f}  {desc}")

    if rep["errors"]:
        lines.append("")
        lines.append("⚠️ 수집실패: " + ", ".join(rep["errors"].keys()))

    lines += ["", "_참고 신호일 뿐. 시스템 진입규칙·리스크관리 우선._"]
    return "\n".join(lines)


def send_telegram(text, token, chat_id):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json=dict(chat_id=chat_id, text=text,
                                     parse_mode="Markdown"), timeout=20)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------
# 6. main
# ------------------------------------------------------------------
def main():
    fred_key = os.environ.get("FRED_API_KEY")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not all([fred_key, tg_token, tg_chat]):
        print("ERROR: FRED_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 필요")
        sys.exit(1)

    rep = build_report(fred_key)
    msg = format_message(rep)
    print(msg)
    send_telegram(msg, tg_token, tg_chat)
    print("\n[sent]")


if __name__ == "__main__":
    main()
