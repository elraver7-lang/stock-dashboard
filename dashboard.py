"""
dashboard.py  —  Real-Time USA Stock Dashboard (Finnhub WebSocket)
Uso:  pip install flask finnhub-python websocket-client
      python dashboard.py  →  http://localhost:5050
"""
import threading, time, json, webbrowser, os
from flask import Flask, jsonify, render_template_string
from datetime import datetime, timedelta, timezone
import finnhub, websocket, yfinance as yf
import pandas as pd
import numpy as np
import requests
import gc
import signal

# Timeout for yfinance calls
yf.set_tz_cache_location("/tmp/yf_cache")

API_KEY        = os.environ.get("FINNHUB_API_KEY", "d764qhpr01qm4b7sv75gd764qhpr01qm4b7sv760")
PORT           = int(os.environ.get("PORT", 5050))
POLL_MS        = 5000
FUND_REFRESH_S = 600

REQUESTED   = ["META","GOOGL","MSFT","NVDA","ORCL","MU","NFLX","SPOT","TSLA","NOW","NU","PAGS","STNE"]
RECOMMENDED = ["AMZN","AMD","V","ASML","JPM","BABA","MELI","ADBE","AVGO","QCOM","CRM","MRVL","TXN","SHOP","UBER","APP","PANW"]
AI_GROWTH   = ["NBIS","ASTS","ONDS","IREN","RKLB","PLTR","ARM","SMCI","CRWD","NET","ANET","IONQ"]
ALL_TICKERS = REQUESTED + RECOMMENDED + AI_GROWTH

# ── Materias Primas & Mineras ─────────────────────────────────────────────────
COMMODITY_ITEMS = [
    # ── Materias primas (Futuros / ETFs puros) ──
    ('GC=F',  'Oro Futuros',              'gold'),
    ('GLD',   'SPDR Gold ETF',            'gold'),
    ('SI=F',  'Plata Futuros',            'silver'),
    ('SLV',   'iShares Silver ETF',       'silver'),
    ('HG=F',  'Cobre Futuros ($/lb)',     'copper'),
    ('COPX',  'Global X Copper Miners ETF','copper'),
    ('URA',   'Global X Uranium ETF',     'uranium'),
    # ── Mineras de Oro ──
    ('GDX',   'VanEck Gold Miners ETF',   'gold-miner'),
    ('NEM',   'Newmont Corp',             'gold-miner'),
    ('GOLD',  'Barrick Gold',             'gold-miner'),
    ('AEM',   'Agnico Eagle Mines',       'gold-miner'),
    ('WPM',   'Wheaton Precious Metals',  'gold-miner'),
    ('KGC',   'Kinross Gold',             'gold-miner'),
    # ── Mineras de Plata ──
    ('PAAS',  'Pan American Silver',      'silver-miner'),
    ('AG',    'First Majestic Silver',    'silver-miner'),
    # ── Mineras de Cobre ──
    ('FCX',   'Freeport-McMoRan',         'copper-miner'),
    ('SCCO',  'Southern Copper Corp',     'copper-miner'),
    ('TECK',  'Teck Resources',           'copper-miner'),
    # ── Mineras de Uranio ──
    ('CCJ',   'Cameco Corp',              'uranium-miner'),
    ('NXE',   'NexGen Energy',            'uranium-miner'),
    ('UEC',   'Uranium Energy Corp',      'uranium-miner'),
    # ── Mineras Diversificadas ──
    ('VALE',  'Vale SA',                  'diversified'),
    ('RIO',   'Rio Tinto',                'diversified'),
    ('BHP',   'BHP Group',                'diversified'),
    ('CLF',   'Cleveland-Cliffs',         'diversified'),
]

_prices     = {}
_info       = {}
_tech       = {}
_sparklines = {}   # ticker -> [price, price, ...]
_market     = {}   # market summary data
_mining     = {}   # mining/commodities data
_options    = {}   # options flow data
_lock       = threading.Lock()
fc = finnhub.Client(api_key=API_KEY)

# ── Top S&P500 tickers para top movers ───────────────────────────────────────
SP500_SAMPLE = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","JPM","V",
    "UNH","XOM","MA","LLY","ORCL","HD","COST","BAC","NFLX","ABBV",
    "CRM","AMD","WMT","CSCO","ACN","MRK","NOW","ADBE","TXN","QCOM",
    "NEE","IBM","GS","INTU","LIN","AMGN","BKNG","ISRG","SPGI","PANW",
    "TMO","AXP","PLD","MDT","MCD","RTX","ADI","MU","INTC","GILD",
    "CVX","SLB","CAT","DE","UPS","LOW","TJX","PGR","REGN","DHR",
    "SHOP","UBER","APP","PLTR","CRWD","NET","ANET","ARM","MRVL","AVGO",
    "PYPL","SNAP","PINS","RBLX","COIN","HOOD","SOFI","RIVN","LCID","F",
    "GE","BA","MMM","HON","ETN","EMR","ROK","PH","DOV","IR"
]

def _compute_bias(hist_d, hist_4h=None):
    """Return (daily_bias, h4_bias) as 'long'/'short'/'neutral'"""
    try:
        close = hist_d['Close']
        if len(close) < 50:
            return 'neutral', 'neutral'
        price = float(close.iloc[-1])
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        rsi   = round(float(_rsi(close)), 1)
        macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        macd_sig  = macd_line.ewm(span=9, adjust=False).mean()
        macd_bull = bool(macd_line.iloc[-1] > macd_sig.iloc[-1])
        bulls_d = sum([price > ema20, price > ema50, rsi > 50, macd_bull])
        bears_d = sum([price < ema20, price < ema50, rsi < 50, not macd_bull])
        if bulls_d >= 3:   daily_bias = 'long'
        elif bears_d >= 3: daily_bias = 'short'
        else:              daily_bias = 'neutral'
    except Exception:
        daily_bias = 'neutral'
        rsi = None

    h4_bias = 'neutral'
    if hist_4h is not None and not hist_4h.empty and len(hist_4h) >= 20:
        try:
            c4 = hist_4h['Close']
            p4 = float(c4.iloc[-1])
            e20_4 = float(c4.ewm(span=20, adjust=False).mean().iloc[-1])
            e50_4 = float(c4.ewm(span=50, adjust=False).mean().iloc[-1])
            rsi4  = float(_rsi(c4))
            ml4   = c4.ewm(span=12, adjust=False).mean() - c4.ewm(span=26, adjust=False).mean()
            ms4   = ml4.ewm(span=9, adjust=False).mean()
            mb4   = bool(ml4.iloc[-1] > ms4.iloc[-1])
            bulls4 = sum([p4 > e20_4, p4 > e50_4, rsi4 > 50, mb4])
            bears4 = sum([p4 < e20_4, p4 < e50_4, rsi4 < 50, not mb4])
            if bulls4 >= 3:   h4_bias = 'long'
            elif bears4 >= 3: h4_bias = 'short'
        except Exception:
            pass

    return daily_bias, h4_bias

def _pivot_levels(hist):
    """Classic floor pivot points from last completed candle"""
    try:
        row = hist.iloc[-2] if len(hist) > 1 else hist.iloc[-1]
        H, L, C = float(row['High']), float(row['Low']), float(row['Close'])
        P  = (H + L + C) / 3
        R1 = round(2*P - L, 2)
        S1 = round(2*P - H, 2)
        R2 = round(P + (H - L), 2)
        S2 = round(P - (H - L), 2)
        R3 = round(H + 2*(P - L), 2)
        S3 = round(L - 2*(H - P), 2)
        return {'pivot': round(P,2), 'R1':R1,'R2':R2,'R3':R3,'S1':S1,'S2':S2,'S3':S3}
    except Exception:
        return None

COUNTRY_FLAGS = {
    'US':'🇺🇸','EU':'🇪🇺','GB':'🇬🇧','JP':'🇯🇵','CN':'🇨🇳',
    'DE':'🇩🇪','FR':'🇫🇷','CA':'🇨🇦','AU':'🇦🇺','NZ':'🇳🇿',
    'CH':'🇨🇭','IT':'🇮🇹','ES':'🇪🇸',
}
MAJOR_COUNTRIES = {'US','EU','GB','JP','CN','DE','FR','CA','AU'}

def _fetch_mktnews():
    """Fetch mktnews flash feed (news + economic indicators)."""
    ts  = int(time.time() * 1000)
    url = f'https://static.mktnews.net/json/flash/en.json?t={ts}'
    r   = requests.get(url, headers={'User-Agent':'Mozilla/5.0'}, timeout=12)
    return r.json() if r.status_code == 200 else []

def _parse_mktnews(items):
    """Split mktnews feed into (news_list, calendar_list)."""
    news, calendar = [], []
    for item in (items or []):
        d = item.get('data') or {}
        t = item.get('type', 0)
        if t == 0:
            content = (d.get('content') or d.get('title') or '').strip()
            if not content or len(content) < 8:
                continue
            impacts = [
                {'impact': imp.get('impact',''), 'symbol': imp.get('symbol','')}
                for imp in (item.get('impact') or [])
                if imp.get('impact') != 'none'
            ]
            news.append({
                'headline':  content[:260],
                'source':    d.get('source','') or 'MKTNews',
                'url':       d.get('source_link','') or '#',
                'image':     d.get('pic','') or '',
                'datetime':  item.get('time',''),
                'important': int(item.get('important', 0)),
                'hot':       bool(item.get('hot', False)),
                'impacts':   impacts,
            })
        elif t == 1:
            country = (d.get('country_code') or '').upper()
            if country not in MAJOR_COUNTRIES:
                continue
            star = int(d.get('star') or 1)
            imp  = 'high' if star >= 3 else ('medium' if star == 2 else 'low')
            calendar.append({
                'time':    d.get('pub_time',''),
                'event':   (d.get('title') or d.get('name') or '').strip(),
                'country': country,
                'flag':    COUNTRY_FLAGS.get(country, '🌐'),
                'impact':  imp,
                'star':    star,
                'actual':  d.get('actual'),
                'estimate':d.get('consensus'),
                'prev':    d.get('previous'),
                'unit':    d.get('unit','') or '',
                'affect':  d.get('affect_status','') or '',
            })
    news.sort(key=lambda x: x['datetime'], reverse=True)
    calendar.sort(key=lambda x: x['time'])
    return news[:25], calendar[:30]

def load_market_data():
    """Load macro market summary: indices, VIX, yields, DXY, sectors, breadth, fear&greed, calendar, movers."""
    print(f"\n[{time.strftime('%H:%M:%S')}] Cargando datos de mercado...")
    result = {}

    # ── Macro instruments ────────────────────────────────────────────────────
    INSTRUMENTS = {
        'sp500': '^GSPC',
        'qqq':   'QQQ',
        'vix':   '^VIX',
        'us10y': '^TNX',
        'us30y': '^TYX',
        'dxy':   'DX=F',
        'gold':  'GC=F',
        'oil':   'CL=F',
    }
    for key, sym in INSTRUMENTS.items():
        try:
            t    = yf.Ticker(sym)
            hist = t.history(period='5d', interval='1d')
            if hist.empty:
                continue
            price = float(hist['Close'].iloc[-1])
            prev  = float(hist['Close'].iloc[-2]) if len(hist) > 1 else price
            chg   = round(price - prev, 4)
            chgp  = round((chg / prev) * 100, 2) if prev else 0
            result[key] = {
                'sym': sym, 'price': round(price, 2),
                'chg': round(chg, 2), 'chgp': chgp,
            }
            if key in ('sp500', 'qqq'):
                hist_4h = None
                try:
                    hist_4h = t.history(period='60d', interval='1h')
                    hist_4h.index = pd.to_datetime(hist_4h.index)
                    hist_4h = hist_4h.resample('4h').agg({
                        'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'
                    }).dropna()
                except Exception:
                    hist_4h = None
                daily_bias, h4_bias = _compute_bias(hist, hist_4h)
                pivots = _pivot_levels(hist)
                result[key]['daily_bias'] = daily_bias
                result[key]['h4_bias']    = h4_bias
                result[key]['pivots']     = pivots
            time.sleep(0.3)
        except Exception as e:
            print(f"  ✗ market {key}: {e}")

    # ── Sector ETFs ──────────────────────────────────────────────────────────
    SECTOR_ETFS = {
        'XLK':'Tecnología','XLF':'Financiero','XLE':'Energía',
        'XLV':'Salud','XLB':'Materiales','XLC':'Comunicaciones',
        'XLI':'Industrial','XLP':'Cons. Básico','XLRE':'Real Estate',
        'XLU':'Utilities','XLY':'Cons. Discr.',
    }
    sectors = []
    try:
        for sym, name in SECTOR_ETFS.items():
            t    = yf.Ticker(sym)
            hist = t.history(period='5d', interval='1d')
            if hist.empty: continue
            price = float(hist['Close'].iloc[-1])
            prev  = float(hist['Close'].iloc[-2]) if len(hist) > 1 else price
            chgp  = round(((price - prev) / prev) * 100, 2) if prev else 0
            sectors.append({'sym': sym, 'name': name, 'price': round(price,2), 'chgp': chgp})
            time.sleep(0.2)
        sectors.sort(key=lambda x: x['chgp'], reverse=True)
    except Exception as e:
        print(f"  ✗ sectors: {e}")
    result['sectors'] = sectors

    # ── Market Breadth ───────────────────────────────────────────────────────
    breadth = {}
    BREADTH_TICKERS = {
        'adv':  '^ADVN',   # NYSE Advancing
        'dec':  '^DECN',   # NYSE Declining
        'nhigh':'^NAHGH',  # NYSE New Highs
        'nlow': '^NALOW',  # NYSE New Lows
        'pcall':'^PCALL',  # CBOE Put/Call Ratio
        'trin': '^TRIN',   # TRIN (Arms Index)
    }
    for key, sym in BREADTH_TICKERS.items():
        try:
            hist = yf.Ticker(sym).history(period='5d', interval='1d')
            if not hist.empty:
                breadth[key] = round(float(hist['Close'].iloc[-1]), 2)
            time.sleep(0.2)
        except Exception:
            pass
    result['breadth'] = breadth

    # ── Fear & Greed Index ────────────────────────────────────────────────────
    # Try CNN API first; if blocked on Render, fall back to synthetic calculation
    # using VIX, Put/Call ratio, market breadth, and SP500 momentum
    fg_result = None
    try:
        r = requests.get(
            'https://production.dataviz.cnn.io/index/fearandgreed/graphdata',
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=8
        )
        fg_data = r.json()
        fg_score  = fg_data.get('fear_and_greed', {}).get('score')
        fg_rating = fg_data.get('fear_and_greed', {}).get('rating', '')
        fg_prev   = fg_data.get('fear_and_greed', {}).get('previous_close')
        if fg_score is not None:
            fg_result = {
                'score':     round(float(fg_score), 1),
                'rating':    fg_rating,
                'prev':      round(float(fg_prev), 1) if fg_prev is not None else None,
                'synthetic': False,
            }
            print(f"  ✓ fear_greed (CNN): {fg_result['score']}")
    except Exception as e:
        print(f"  ✗ CNN fear_greed: {e}")

    # Synthetic fallback — computed from available indicators
    if fg_result is None:
        try:
            components = []
            # 1. VIX component (inverted): VIX 10→100 (greed), VIX 20→50 (neutral), VIX 40→0 (fear)
            vix_price = result.get('vix', {}).get('price')
            if vix_price is not None:
                vix_score = max(0.0, min(100.0, (40.0 - float(vix_price)) / 30.0 * 100.0))
                components.append(('VIX', vix_score))

            # 2. Put/Call ratio: 0.5→100 (greed), 0.9→50 (neutral), 1.3→0 (fear)
            pcall = result.get('breadth', {}).get('pcall')
            if pcall is not None:
                pc_score = max(0.0, min(100.0, (1.3 - float(pcall)) / 0.8 * 100.0))
                components.append(('P/C', pc_score))

            # 3. Advance/Decline breadth: ratio > 3 → greed, ratio 1 → neutral, < 0.4 → fear
            adv = result.get('breadth', {}).get('adv')
            dec = result.get('breadth', {}).get('dec')
            if adv and dec and dec > 0:
                ratio = float(adv) / float(dec)
                breadth_score = max(0.0, min(100.0, 50.0 + (ratio - 1.0) * 20.0))
                components.append(('Breadth', breadth_score))

            # 4. New Highs vs New Lows: all highs→100, all lows→0
            nhigh = result.get('breadth', {}).get('nhigh')
            nlow  = result.get('breadth', {}).get('nlow')
            if nhigh is not None and nlow is not None and (nhigh + nlow) > 0:
                hl_score = (float(nhigh) / (float(nhigh) + float(nlow))) * 100.0
                components.append(('H/L', hl_score))

            # 5. TRIN (Arms Index): <0.8 → greed, 1.0 → neutral, >1.5 → fear
            trin = result.get('breadth', {}).get('trin')
            if trin is not None:
                trin_score = max(0.0, min(100.0, (1.5 - float(trin)) / 0.7 * 100.0))
                components.append(('TRIN', trin_score))

            # 6. SP500 momentum: price vs 20-day avg change
            sp500 = result.get('sp500', {})
            sp_chgp = sp500.get('chgp')
            if sp_chgp is not None:
                mom_score = max(0.0, min(100.0, 50.0 + float(sp_chgp) * 10.0))
                components.append(('Momentum', mom_score))

            if components:
                score = round(sum(v for _, v in components) / len(components), 1)
                if score >= 75:   rating = 'Extreme Greed'
                elif score >= 55: rating = 'Greed'
                elif score >= 45: rating = 'Neutral'
                elif score >= 25: rating = 'Fear'
                else:             rating = 'Extreme Fear'
                fg_result = {'score': score, 'rating': rating, 'prev': None, 'synthetic': True}
                print(f"  ✓ fear_greed (sintético {len(components)} indicadores): {score} — {rating}")
            else:
                print(f"  ✗ fear_greed sintético: sin datos disponibles")
        except Exception as e:
            print(f"  ✗ fear_greed sintético: {e}")

    result['fear_greed'] = fg_result

    # ── News + Economic Calendar via mktnews.com ──────────────────────────────
    try:
        mktnews_items         = _fetch_mktnews()
        mkt_news, mkt_cal     = _parse_mktnews(mktnews_items)
        result['news']        = mkt_news
        result['econ_calendar'] = mkt_cal
        print(f"  ✓ mktnews: {len(mkt_news)} noticias, {len(mkt_cal)} eventos de calendario")
    except Exception as e:
        print(f"  ✗ mktnews: {e}")
        result['news']          = []
        result['econ_calendar'] = []

    # ── Earnings Calendar (próximos 14 días para tickers seguidos) ────────────
    try:
        now = datetime.now()
        earnings_raw = fc.earnings_calendar(
            _from=now.strftime('%Y-%m-%d'),
            to=(now + timedelta(days=14)).strftime('%Y-%m-%d'),
            symbol=None, international=False
        )
        tracked_set = set(ALL_TICKERS)
        earnings = []
        for e in (earnings_raw.get('earningsCalendar') or []):
            sym = e.get('symbol','')
            if sym not in tracked_set: continue
            earnings.append({
                'ticker': sym,
                'date':   e.get('date',''),
                'hour':   e.get('hour',''),          # bmo / amc / dmh
                'eps_est':e.get('epsEstimate'),
                'eps_act':e.get('epsActual'),
                'rev_est':e.get('revenueEstimate'),
            })
        earnings.sort(key=lambda x: x['date'])
        result['earnings'] = earnings
    except Exception as e:
        print(f"  ✗ earnings: {e}")
        result['earnings'] = []

    # ── Pre-market movers ─────────────────────────────────────────────────────
    pre_movers = []
    try:
        pre_batch = yf.download(
            SP500_SAMPLE, period='2d', interval='1d',
            prepost=True, group_by='ticker',
            auto_adjust=True, progress=False, threads=True
        )
        for ticker in SP500_SAMPLE:
            try:
                if ticker not in pre_batch.columns.get_level_values(0): continue
                closes = pre_batch[ticker]['Close'].dropna()
                if len(closes) < 2: continue
                prev_c = float(closes.iloc[-2])
                last_c = float(closes.iloc[-1])
                if prev_c == 0: continue
                chgp = round(((last_c - prev_c) / prev_c) * 100, 2)
                if abs(chgp) >= 1.0:
                    pre_movers.append({'ticker': ticker, 'price': round(last_c,2), 'chgp': chgp})
            except Exception:
                pass
        pre_movers.sort(key=lambda x: abs(x['chgp']), reverse=True)
    except Exception as e:
        print(f"  ✗ pre_movers: {e}")
    result['pre_movers'] = pre_movers[:20]

    # ── Top movers (S&P500 sample — sesión previa) ───────────────────────────
    movers = []
    try:
        batch = yf.download(
            SP500_SAMPLE, period='2d', interval='1d',
            group_by='ticker', auto_adjust=True, progress=False, threads=True
        )
        for ticker in SP500_SAMPLE:
            try:
                if ticker not in batch.columns.get_level_values(0): continue
                closes = batch[ticker]['Close'].dropna()
                if len(closes) < 2: continue
                prev_c = float(closes.iloc[-2])
                last_c = float(closes.iloc[-1])
                if prev_c == 0: continue
                chgp = round(((last_c - prev_c) / prev_c) * 100, 2)
                movers.append({'ticker': ticker, 'price': round(last_c,2), 'chgp': chgp})
            except Exception:
                pass
        movers.sort(key=lambda x: x['chgp'], reverse=True)
        result['top_gainers'] = movers[:15]
        result['top_losers']  = movers[-15:][::-1]
    except Exception as e:
        print(f"  ✗ top_movers: {e}")
        result['top_gainers'] = []
        result['top_losers']  = []

    result['updated_at'] = time.strftime("%d/%m/%Y  %H:%M:%S")
    with _lock:
        _market.update(result)
    print(f"[{time.strftime('%H:%M:%S')}] Datos de mercado listos ✓")

def market_loop():
    while True:
        time.sleep(600)
        gc.collect()
        try:
            load_market_data()
        except Exception as e:
            print(f"[market_loop] Error: {e}")

def load_mining_data():
    """Fetch prices, 30d range, 52w range, RSI-14, ATR-14 for commodities & miners."""
    print(f"\n[{time.strftime('%H:%M:%S')}] Cargando datos de materias primas y mineras...")
    items_out = []
    for ticker, name, asset_type in COMMODITY_ITEMS:
        try:
            t    = yf.Ticker(ticker)
            hist = t.history(period='1y', interval='1d')
            if hist.empty or len(hist) < 5:
                continue
            close  = hist['Close']
            price  = float(close.iloc[-1])
            prev   = float(close.iloc[-2]) if len(close) > 1 else price
            chg    = round(price - prev, 6)
            chgp   = round((chg / prev) * 100, 2) if prev else 0

            # ── 30-day range ────────────────────────────────────────────────
            h30    = hist.tail(30)
            hi30   = float(h30['High'].max())
            lo30   = float(h30['Low'].min())
            d30h   = round(((price - hi30) / hi30) * 100, 1) if hi30 else None  # ≤0
            d30l   = round(((price - lo30) / lo30) * 100, 1) if lo30 else None  # ≥0
            # position in 30d range 0=min 100=max
            rng30  = hi30 - lo30
            pos30  = round(((price - lo30) / rng30) * 100, 1) if rng30 > 0 else 50

            # ── 52-week range ───────────────────────────────────────────────
            hi52   = float(hist['High'].max())
            lo52   = float(hist['Low'].min())
            d52h   = round(((price - hi52) / hi52) * 100, 1) if hi52 else None
            rng52  = hi52 - lo52
            pos52  = round(((price - lo52) / rng52) * 100, 1) if rng52 > 0 else 50

            # ── RSI-14 ──────────────────────────────────────────────────────
            rsi = round(float(_rsi(close)), 1) if len(close) >= 15 else None

            # ── ATR-14 ──────────────────────────────────────────────────────
            atr14 = None
            atr_pct = None
            if len(hist) >= 15:
                hi = hist['High']; lo2 = hist['Low']; pc = close.shift(1)
                tr  = pd.concat([hi - lo2, (hi - pc).abs(), (lo2 - pc).abs()], axis=1).max(axis=1)
                atr14   = round(float(tr.ewm(span=14, adjust=False).mean().iloc[-1]), 4)
                atr_pct = round((atr14 / price) * 100, 2) if price else None

            # ── Volume (only stocks/ETFs, not futures) ──────────────────────
            volume = None
            if '=F' not in ticker:
                try:
                    volume = int(hist['Volume'].iloc[-1])
                except Exception:
                    pass

            items_out.append({
                'ticker':  ticker,
                'name':    name,
                'type':    asset_type,
                'price':   round(price, 4),
                'chg':     round(chg, 4),
                'chgp':    chgp,
                'hi30':    round(hi30, 4),
                'lo30':    round(lo30, 4),
                'd30h':    d30h,
                'd30l':    d30l,
                'pos30':   pos30,
                'hi52':    round(hi52, 4),
                'lo52':    round(lo52, 4),
                'd52h':    d52h,
                'pos52':   pos52,
                'rsi14':   rsi,
                'atr14':   atr14,
                'atr_pct': atr_pct,
                'volume':  volume,
            })
            print(f"  ✓ {ticker:6s}  ${price:.3f}  RSI={rsi}  ATR%={atr_pct}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  ✗ mining {ticker}: {e}")

    with _lock:
        _mining['items']      = items_out
        _mining['updated_at'] = time.strftime("%d/%m/%Y  %H:%M:%S")
    print(f"[{time.strftime('%H:%M:%S')}] Materias primas listas: {len(items_out)} activos ✓")

def mining_loop():
    while True:
        time.sleep(600)
        gc.collect()
        try:
            load_mining_data()
        except Exception as e:
            print(f"[mining_loop] Error: {e}")

# ── Options helpers ───────────────────────────────────────────────────────────
# BTC uses Deribit (yfinance BTC-USD has no options market)
# SPY, SLV, GGAL use yfinance / CBOE data
OPTIONS_YF_TICKERS  = {'SPY':'SPY — S&P 500 ETF', 'SLV':'Silver ETF (SLV)', 'GGAL':'Galicia (GGAL)'}
OPTIONS_ALL_LABELS  = {'SPY':'SPY — S&P 500 ETF', 'BTC':'Bitcoin (BTC) — Deribit',
                       'SLV':'Silver ETF (SLV)',   'GGAL':'Galicia (GGAL)'}

def _exp_flags(dt):
    """Is this expiry monthly (3rd Friday) or quarterly (3rd Friday of Mar/Jun/Sep/Dec)?"""
    is_m = (dt.weekday() == 4 and 14 < dt.day <= 21)
    is_q = is_m and dt.month in (3, 6, 9, 12)
    return is_m, is_q

def _max_pain_np(c_k, c_oi, p_k, p_oi):
    """Vectorized max-pain via numpy."""
    try:
        strikes = np.unique(np.concatenate([c_k, p_k]))
        if not len(strikes): return None
        pain = np.zeros(len(strikes))
        if len(c_k): pain += np.array([np.sum(np.maximum(0, sp - c_k) * c_oi) for sp in strikes])
        if len(p_k): pain += np.array([np.sum(np.maximum(0, p_k - sp) * p_oi) for sp in strikes])
        return float(round(strikes[np.argmin(pain)], 2))
    except Exception:
        return None

def _gamma_levels_df(calls_df, puts_df, current_price, n=10):
    """
    Fast vectorized gamma levels with flow signal.
    Flow score 0-1: near 0 = last trade at bid (VENDIDO/sold), near 1 = last at ask (COMPRADO/bought).
    """
    try:
        parts = []
        for df, side in [(calls_df, 'c'), (puts_df, 'p')]:
            if df is None or df.empty: continue
            cols = set(df.columns)
            # filter ±22% from current price to reduce SPY's 400+ strikes
            if current_price and current_price > 0:
                df = df[df['strike'].between(current_price * 0.78, current_price * 1.22)]
            if df.empty: continue

            sub = pd.DataFrame()
            sub['strike'] = df['strike'].round(2).values
            sub['oi']     = df['openInterest'].fillna(0).astype(int).values  if 'openInterest'       in cols else 0
            sub['vol']    = df['volume'].fillna(0).astype(int).values         if 'volume'             in cols else 0
            sub['iv']     = (df['impliedVolatility'].fillna(0)*100).round(1).values if 'impliedVolatility' in cols else 0.0
            sub['bid']    = df['bid'].fillna(0).values                         if 'bid'               in cols else 0.0
            sub['ask']    = df['ask'].fillna(0).values                         if 'ask'               in cols else 0.0
            sub['last']   = df['lastPrice'].fillna(0).values                   if 'lastPrice'         in cols else 0.0
            sub['itm']    = df['inTheMoney'].fillna(False).astype(bool).values if 'inTheMoney'        in cols else False
            sub['side']   = side
            parts.append(sub)

        if not parts: return []
        all_d = pd.concat(parts, ignore_index=True)

        # Flow score: (last - bid) / (ask - bid), clipped [0,1]. Only valid when spread > 0
        spread = all_d['ask'] - all_d['bid']
        valid  = (spread > 0.001) & (all_d['last'] > 0)
        all_d['flow'] = np.where(valid, ((all_d['last'] - all_d['bid']) / spread).clip(0, 1), np.nan)

        # Group by (strike, side)
        g = all_d.groupby(['strike','side']).agg(
            oi=('oi','sum'), vol=('vol','sum'),
            iv=('iv','max'),
            flow=('flow','mean'),  # mean of flow scores across same strike
            itm=('itm','any'),
        ).reset_index()

        # Pivot to one row per strike
        calls_g = g[g['side']=='c'].set_index('strike').add_prefix('c_').drop(columns=['c_side'])
        puts_g  = g[g['side']=='p'].set_index('strike').add_prefix('p_').drop(columns=['p_side'])
        merged  = calls_g.join(puts_g, how='outer').fillna({'c_oi':0,'p_oi':0,'c_vol':0,'p_vol':0,'c_iv':0,'p_iv':0})
        merged['total_oi']  = merged['c_oi'] + merged['p_oi']
        merged['total_vol'] = merged['c_vol'] + merged['p_vol']
        merged = merged.nlargest(n, 'total_oi')

        items = []
        for strike_val, row in merged.iterrows():
            dist = round(((strike_val - current_price) / current_price)*100, 1) if current_price else None
            dominant = 'call' if row['c_oi'] >= row['p_oi'] else 'put'
            if dist is None:              moneyness = '—'
            elif abs(dist) <= 1.5:        moneyness = 'ATM'
            elif strike_val > current_price: moneyness = 'OTM' if dominant == 'call' else 'ITM'
            else:                          moneyness = 'ITM' if dominant == 'call' else 'OTM'

            def safe_flow(v):
                try: return round(float(v), 2) if not np.isnan(v) else None
                except: return None

            items.append({
                'strike':    round(float(strike_val), 2),
                'c_oi':      int(row['c_oi']),
                'p_oi':      int(row['p_oi']),
                'c_vol':     int(row.get('c_vol', 0)),
                'p_vol':     int(row.get('p_vol', 0)),
                'c_iv':      round(float(row['c_iv']), 1) if row.get('c_iv') else None,
                'p_iv':      round(float(row['p_iv']), 1) if row.get('p_iv') else None,
                'c_flow':    safe_flow(row.get('c_flow', float('nan'))),
                'p_flow':    safe_flow(row.get('p_flow', float('nan'))),
                'total_oi':  int(row['total_oi']),
                'total_vol': int(row['total_vol']),
                'dominant':  dominant,
                'dist_pct':  dist,
                'moneyness': moneyness,
            })
        return items
    except Exception as e:
        print(f"  ✗ _gamma_levels_df: {e}")
        return []

def _process_chain_expiry(t, exp, cur_price, exp_info):
    """Fetch + process a single expiry date from yfinance; returns expiry dict or None."""
    try:
        chain  = t.option_chain(exp)
        calls, puts = chain.calls, chain.puts

        def safe_col(df, col):
            return df[col].fillna(0) if col in df.columns else pd.Series(0, index=df.index)

        c_oi   = int(safe_col(calls,'openInterest').sum())
        p_oi   = int(safe_col(puts, 'openInterest').sum())
        c_vol  = int(safe_col(calls,'volume').sum())
        p_vol  = int(safe_col(puts, 'volume').sum())
        pc_oi  = round(p_oi / c_oi,  2) if c_oi  > 0 else None
        pc_vol = round(p_vol / c_vol, 2) if c_vol > 0 else None

        c_k  = calls['strike'].values         if not calls.empty else np.array([])
        c_oi_v = safe_col(calls,'openInterest').values if not calls.empty else np.array([])
        p_k  = puts['strike'].values          if not puts.empty else np.array([])
        p_oi_v = safe_col(puts, 'openInterest').values if not puts.empty else np.array([])
        mp   = _max_pain_np(c_k, c_oi_v, p_k, p_oi_v)
        mp_dist = round(((mp - cur_price) / cur_price)*100, 1) if mp and cur_price else None

        levels = _gamma_levels_df(calls, puts, cur_price, n=10)
        dte    = max((datetime.strptime(exp,'%Y-%m-%d') - datetime.now()).days, 0)

        return {
            'date': exp, 'dte': dte,
            'monthly': exp_info['monthly'], 'quarterly': exp_info['quarterly'],
            'c_oi': c_oi, 'p_oi': p_oi, 'c_vol': c_vol, 'p_vol': p_vol,
            'pc_oi': pc_oi, 'pc_vol': pc_vol,
            'max_pain': mp, 'mp_dist': mp_dist, 'levels': levels,
        }
    except Exception as e:
        print(f"    ✗ {exp}: {e}")
        return None

def _load_yf_options(sym, label, n_exp=5):
    """Load options for a single yfinance-supported ticker."""
    try:
        t       = yf.Ticker(sym)
        all_exp = t.options or []
        if not all_exp:
            print(f"    ✗ {sym}: sin vencimientos en yfinance")
            return None

        cur_price = None
        try:
            h = t.history(period='2d', interval='1d')
            if not h.empty: cur_price = round(float(h['Close'].iloc[-1]), 4)
        except Exception: pass

        expiries_out, total_c, total_p = [], 0, 0
        for exp in all_exp[:n_exp]:
            dt        = datetime.strptime(exp,'%Y-%m-%d')
            is_m, is_q = _exp_flags(dt)
            info      = {'date': exp, 'monthly': is_m, 'quarterly': is_q}
            row       = _process_chain_expiry(t, exp, cur_price, info)
            if row:
                expiries_out.append(row)
                total_c += row['c_oi']
                total_p += row['p_oi']
            time.sleep(0.8)

        agg_pc = round(total_p / total_c, 2) if total_c > 0 else None
        print(f"    ✓ {sym}  {len(expiries_out)} vencimientos  P/C={agg_pc}")
        return {
            'label': label, 'price': cur_price,
            'expiries': expiries_out,
            'total_call_oi_all': total_c, 'total_put_oi_all': total_p,
            'agg_pc': agg_pc, 'source': 'yfinance',
        }
    except Exception as e:
        print(f"    ✗ {sym}: {e}")
        return None

def _load_btc_options_deribit():
    """Fetch BTC options from Deribit public API (no auth required)."""
    import re as _re
    MONTHS = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
              'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
    try:
        r = requests.get(
            'https://www.deribit.com/api/v2/public/get_book_summary_by_currency',
            params={'currency':'BTC','kind':'option'},
            headers={'User-Agent':'Mozilla/5.0'}, timeout=20
        )
        items = r.json().get('result', [])
        if not items:
            print("    ✗ BTC Deribit: sin datos"); return None
    except Exception as e:
        print(f"    ✗ BTC Deribit fetch: {e}"); return None

    # Current BTC price from yfinance as fallback
    cur_price = None
    try:
        h = yf.Ticker('BTC-USD').history(period='2d', interval='1d')
        if not h.empty: cur_price = round(float(h['Close'].iloc[-1]), 0)
    except Exception: pass

    # Group by expiry
    from collections import defaultdict
    by_exp = defaultdict(lambda: {'c':[], 'p':[]})
    for item in items:
        name = item.get('instrument_name','')
        parts = name.split('-')
        if len(parts) != 4: continue
        m2 = _re.match(r'(\d+)([A-Z]+)(\d+)', parts[1])
        if not m2 or m2.group(2) not in MONTHS: continue
        d, mo, yr = int(m2.group(1)), MONTHS[m2.group(2)], int('20'+m2.group(3)) if len(m2.group(3))==2 else int(m2.group(3))
        exp_str = f"{yr}-{mo:02d}-{d:02d}"
        side    = 'c' if parts[3]=='C' else 'p'
        by_exp[exp_str][side].append({
            'strike': float(parts[2]),
            'oi':     float(item.get('open_interest',0)),
            'vol':    float(item.get('volume',0)),
            'iv':     float(item.get('mark_iv',0)),
        })

    expiries_out, total_c, total_p = [], 0, 0
    for exp in sorted(by_exp.keys())[:6]:
        calls_list = by_exp[exp]['c']
        puts_list  = by_exp[exp]['p']
        c_oi  = int(sum(x['oi'] for x in calls_list))
        p_oi  = int(sum(x['oi'] for x in puts_list))
        c_vol = int(sum(x['vol'] for x in calls_list))
        p_vol = int(sum(x['vol'] for x in puts_list))
        pc_oi = round(p_oi/c_oi, 2) if c_oi > 0 else None

        # max pain via numpy
        if calls_list and puts_list:
            c_k   = np.array([x['strike'] for x in calls_list])
            c_oiv = np.array([x['oi']     for x in calls_list])
            p_k   = np.array([x['strike'] for x in puts_list])
            p_oiv = np.array([x['oi']     for x in puts_list])
            mp    = _max_pain_np(c_k, c_oiv, p_k, p_oiv)
        else:
            mp = None
        mp_dist = round(((mp - cur_price)/cur_price)*100, 1) if mp and cur_price else None

        # key OI levels (filter ±25% and top 10)
        all_strikes = {}
        for x in calls_list:
            k = x['strike']
            if cur_price and (k < cur_price*0.75 or k > cur_price*1.25): continue
            all_strikes.setdefault(k, {'strike':k,'c_oi':0,'p_oi':0,'c_vol':0,'p_vol':0,'c_iv':None,'p_iv':None,'c_flow':None,'p_flow':None,'total_oi':0,'total_vol':0,'dominant':'call','dist_pct':None,'moneyness':'—'})
            all_strikes[k]['c_oi']  += int(x['oi'])
            all_strikes[k]['c_vol'] += int(x['vol'])
            if x['iv']: all_strikes[k]['c_iv'] = round(x['iv'], 1)
        for x in puts_list:
            k = x['strike']
            if cur_price and (k < cur_price*0.75 or k > cur_price*1.25): continue
            all_strikes.setdefault(k, {'strike':k,'c_oi':0,'p_oi':0,'c_vol':0,'p_vol':0,'c_iv':None,'p_iv':None,'c_flow':None,'p_flow':None,'total_oi':0,'total_vol':0,'dominant':'put','dist_pct':None,'moneyness':'—'})
            all_strikes[k]['p_oi']  += int(x['oi'])
            all_strikes[k]['p_vol'] += int(x['vol'])
            if x['iv']: all_strikes[k]['p_iv'] = round(x['iv'], 1)
        for v in all_strikes.values():
            v['total_oi'] = v['c_oi'] + v['p_oi']
            v['total_vol']= v['c_vol']+ v['p_vol']
            v['dominant'] = 'call' if v['c_oi'] >= v['p_oi'] else 'put'
            if cur_price:
                v['dist_pct'] = round(((v['strike']-cur_price)/cur_price)*100,1)
                d = abs(v['dist_pct'])
                if d <= 1.5: v['moneyness']='ATM'
                elif v['strike']>cur_price: v['moneyness']='OTM' if v['dominant']=='call' else 'ITM'
                else: v['moneyness']='ITM' if v['dominant']=='call' else 'OTM'
        levels = sorted(all_strikes.values(), key=lambda x: x['total_oi'], reverse=True)[:10]

        dt = datetime.strptime(exp,'%Y-%m-%d')
        is_m, is_q = _exp_flags(dt)
        dte = max((dt - datetime.now()).days, 0)
        expiries_out.append({
            'date':exp,'dte':dte,'monthly':is_m,'quarterly':is_q,
            'c_oi':c_oi,'p_oi':p_oi,'c_vol':c_vol,'p_vol':p_vol,
            'pc_oi':pc_oi,'pc_vol':None,'max_pain':mp,'mp_dist':mp_dist,'levels':levels,
        })
        total_c += c_oi; total_p += p_oi

    if not expiries_out:
        print("    ✗ BTC Deribit: sin expiries procesados"); return None

    agg_pc = round(total_p/total_c,2) if total_c > 0 else None
    print(f"    ✓ BTC Deribit  {len(expiries_out)} vencimientos  P/C={agg_pc}  (OI en contratos BTC)")
    return {
        'label': 'Bitcoin (BTC) — Deribit', 'price': cur_price,
        'expiries': expiries_out,
        'total_call_oi_all': total_c, 'total_put_oi_all': total_p,
        'agg_pc': agg_pc, 'source': 'deribit', 'oi_unit': 'contratos BTC',
    }

def load_options_data():
    """Fetch options for SPY, BTC (Deribit), SLV, GGAL."""
    print(f"\n[{time.strftime('%H:%M:%S')}] Cargando datos de opciones...")
    result = {}

    # yfinance tickers
    for sym, label in OPTIONS_YF_TICKERS.items():
        print(f"  → {sym}")
        data = _load_yf_options(sym, label, n_exp=5)
        if data: result[sym] = data
        time.sleep(1.5)

    # BTC via Deribit
    print(f"  → BTC (Deribit)")
    btc_data = _load_btc_options_deribit()
    if btc_data: result['BTC'] = btc_data

    with _lock:
        _options['data']       = result
        _options['updated_at'] = time.strftime("%d/%m/%Y  %H:%M:%S")
    print(f"[{time.strftime('%H:%M:%S')}] Opciones listas ✓  ({list(result.keys())})")

def options_loop():
    while True:
        time.sleep(900)
        gc.collect()
        try:
            load_options_data()
        except Exception as e:
            print(f"[options_loop] Error: {e}")

# ── technical helpers ─────────────────────────────────────────────────────────
def _rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period-1, min_periods=period).mean()
    avg_l = loss.ewm(com=period-1, min_periods=period).mean()
    rs    = avg_g / avg_l.replace(0, float('inf'))
    return (100 - (100 / (1 + rs))).iloc[-1]

def load_technicals():
    for ticker in ALL_TICKERS:
        try:
            t = yf.Ticker(ticker)
            hist_d = t.history(period="18mo", interval="1d")
            hist_w = t.history(period="3y",   interval="1wk")
            if hist_d.empty or len(hist_d) < 30:
                continue
            close_d = hist_d['Close']
            price   = float(close_d.iloc[-1])

            rsi_d  = round(float(_rsi(close_d)), 1)
            rsi_w  = round(float(_rsi(hist_w['Close'])), 1) if len(hist_w) >= 15 else None
            ema200 = float(close_d.ewm(span=200, adjust=False).mean().iloc[-1]) if len(close_d) >= 100 else None
            ema50  = float(close_d.ewm(span=50,  adjust=False).mean().iloc[-1]) if len(close_d) >= 30  else None
            pct_e200 = round(((price - ema200) / ema200) * 100, 1) if ema200 else None

            ema12   = close_d.ewm(span=12, adjust=False).mean()
            ema26   = close_d.ewm(span=26, adjust=False).mean()
            macd_l  = ema12 - ema26
            sig_l   = macd_l.ewm(span=9, adjust=False).mean()
            macd_bull = bool(macd_l.iloc[-1] > sig_l.iloc[-1])
            macd_hist = round(float(macd_l.iloc[-1] - sig_l.iloc[-1]), 2)

            golden = bool(ema50 and ema200 and ema50 > ema200)

            # ATR 14
            atr14 = None
            if len(hist_d) >= 15:
                high = hist_d['High']
                low  = hist_d['Low']
                pc   = close_d.shift(1)
                tr   = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
                atr14 = round(float(tr.ewm(span=14, adjust=False).mean().iloc[-1]), 2)

            # composite tech signal: count bullish vs bearish factors
            bulls = sum([
                rsi_d < 70,                     # not overbought daily
                rsi_w is None or rsi_w < 70,    # not overbought weekly
                pct_e200 is not None and pct_e200 > -5,  # near or above EMA200
                golden,                          # golden cross
                macd_bull,                       # MACD bullish
            ])
            bears = sum([
                rsi_d > 70,
                rsi_w is not None and rsi_w > 70,
                pct_e200 is not None and pct_e200 < -10,
                not golden,
                not macd_bull,
            ])
            if bulls >= 4:    signal = 'buy'
            elif bears >= 4:  signal = 'sell'
            else:             signal = 'neutral'

            with _lock:
                _tech[ticker] = {
                    'rsi_d':    rsi_d,
                    'rsi_w':    rsi_w,
                    'ema200':   round(ema200, 2) if ema200 else None,
                    'ema50':    round(ema50, 2)  if ema50  else None,
                    'pct_e200': pct_e200,
                    'golden':   golden,
                    'macd_bull':macd_bull,
                    'macd_hist':macd_hist,
                    'signal':   signal,
                    'atr14':    atr14,
                }
            print(f"  📊 {ticker:6s}  RSI-D={rsi_d:.0f}  EMA200={'%+.1f%%'%pct_e200 if pct_e200 else '?'}  {'🟢' if signal=='buy' else '🔴' if signal=='sell' else '⚪'}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  ✗ tech {ticker}: {e}")

def technicals_loop():
    while True:
        time.sleep(900)
        gc.collect()
        try:
            print(f"\n[{time.strftime('%H:%M:%S')}] Refrescando indicadores técnicos...")
            load_technicals()
        except Exception as e:
            print(f"[technicals_loop] Error: {e}")

def load_sparklines():
    for ticker in ALL_TICKERS:
        try:
            hist = yf.Ticker(ticker).history(period="7d", interval="1h")
            if not hist.empty:
                prices = [round(float(p), 2) for p in hist['Close'].tolist()]
                with _lock:
                    _sparklines[ticker] = prices
            time.sleep(0.2)
        except Exception as e:
            print(f"  ✗ spark {ticker}: {e}")

def sparklines_loop():
    while True:
        time.sleep(1800)
        gc.collect()
        try:
            print(f"\n[{time.strftime('%H:%M:%S')}] Refrescando sparklines...")
            load_sparklines()
        except Exception as e:
            print(f"[sparklines_loop] Error: {e}")

def safe(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict): d = d.get(k)
        else: return default
    return d if d is not None else default

def load_fundamentals():
    for ticker in ALL_TICKERS:
        try:
            t    = yf.Ticker(ticker)
            info = t.info or {}

            price      = info.get('currentPrice') or info.get('regularMarketPrice')
            prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
            mktcap     = info.get('marketCap') or 0
            mktcap_b   = round(mktcap / 1e9, 1) if mktcap else None

            # analyst score: yfinance recommendationMean 1=Strong Buy … 5=Strong Sell
            analyst_score = None
            analyst_buy = analyst_hold = analyst_sell = None
            raw_score = info.get('recommendationMean')
            n_analysts = info.get('numberOfAnalystOpinions') or 0
            if raw_score and n_analysts:
                # invert to our 1-5 scale where 5=Strong Buy
                analyst_score = round(6 - float(raw_score), 2)
            # try detailed breakdown
            try:
                rec_df = t.recommendations
                if rec_df is not None and not rec_df.empty:
                    r = rec_df.iloc[0]
                    sb = int(r.get('strongBuy', 0)); b = int(r.get('buy', 0))
                    h  = int(r.get('hold', 0))
                    ss = int(r.get('strongSell', 0)); sl = int(r.get('sell', 0))
                    total = sb+b+h+ss+sl
                    if total:
                        analyst_buy   = sb+b
                        analyst_hold  = h
                        analyst_sell  = ss+sl
                        if not analyst_score:
                            analyst_score = round((sb*5+b*4+h*3+sl*2+ss*1)/total, 2)
            except Exception: pass

            # price target
            price_target = None
            try:
                pt = info.get('targetMedianPrice') or info.get('targetMeanPrice')
                if pt: price_target = round(float(pt), 2)
            except Exception: pass

            # fallback: use Finnhub only for price_target if yf didn't have it
            if not price_target:
                try:
                    ft = fc.price_target(symbol=ticker)
                    v  = ft.get('targetMedian') or ft.get('targetMean')
                    if v: price_target = round(float(v), 2)
                    time.sleep(1.0)   # 1 Finnhub call, safe within rate limit
                except Exception: pass

            # RVOL — relative volume vs 20-day avg, adjusted by time of day
            avg_vol = info.get('averageVolume') or info.get('averageDailyVolume10Day') or 0
            cur_vol = info.get('volume') or 0
            rvol = None
            if avg_vol > 0 and cur_vol > 0:
                try:
                    from datetime import timezone as _tz
                    import datetime as _dt
                    et_offset = _dt.timezone(_dt.timedelta(hours=-4))  # EDT
                    et_now = datetime.now(et_offset)
                    if et_now.hour >= 9 and (et_now.hour > 9 or et_now.minute >= 30) and et_now.hour < 16:
                        mins = (et_now.hour - 9) * 60 + et_now.minute - 30
                        mins = max(mins, 15)
                        rvol = round(cur_vol / (avg_vol * mins / 390), 2)
                    else:
                        rvol = round(cur_vol / avg_vol, 2)
                except Exception:
                    rvol = round(cur_vol / avg_vol, 2) if avg_vol else None

            with _lock:
                _info[ticker] = {
                    'name':         (info.get('longName') or info.get('shortName') or ticker)[:34],
                    'currency':     info.get('currency', 'USD'),
                    'mktcap_b':     mktcap_b,
                    'sector':       info.get('sector') or info.get('industry') or '—',
                    'pe_trail':     info.get('trailingPE'),
                    'pe_fwd':       info.get('forwardPE'),
                    'pb':           info.get('priceToBook'),
                    'ps':           info.get('priceToSalesTrailing12Months'),
                    'eps':          info.get('trailingEps'),
                    'eps_growth':   info.get('earningsGrowth'),
                    'rev_growth':   info.get('revenueGrowth'),
                    'gross_margin': info.get('grossMargins'),
                    'net_margin':   info.get('profitMargins'),
                    'roe':          info.get('returnOnEquity'),
                    'roa':          info.get('returnOnAssets'),
                    'debt_eq':      info.get('debtToEquity'),
                    'beta':         info.get('beta'),
                    'div_yield':    info.get('dividendYield'),
                    'w52h':         info.get('fiftyTwoWeekHigh'),
                    'w52l':         info.get('fiftyTwoWeekLow'),
                    'price_target': price_target,
                    'analyst_buy':  analyst_buy,
                    'analyst_hold': analyst_hold,
                    'analyst_sell': analyst_sell,
                    'analyst_score':analyst_score,
                    'rvol':         rvol,
                    'avg_volume':   avg_vol,
                }
                if price:
                    chg  = round(float(price) - float(prev_close), 2) if prev_close else None
                    chgp = round((chg / float(prev_close)) * 100, 2) if chg and prev_close else None
                    _prices[ticker] = {
                        'price':      round(float(price), 2),
                        'change':     chg,
                        'changepct':  chgp,
                        'prev_close': float(prev_close) if prev_close else None,
                        'high_day':   info.get('dayHigh'),
                        'low_day':    info.get('dayLow'),
                        'volume':     info.get('volume'),
                        'source':     'yf',
                    }
            print(f"  ✓ {ticker:6s}  ${price:.2f}" if price else f"  ? {ticker}")
            time.sleep(0.4)
        except Exception as e:
            print(f"  ✗ {ticker}: {e}")

def fundamentals_loop():
    while True:
        time.sleep(FUND_REFRESH_S)
        gc.collect()
        try:
            import resource
            mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            print(f"  [MEM] {mem_mb:.0f} MB")
        except: pass
        print(f"\n[{time.strftime('%H:%M:%S')}] Refrescando fundamentals...")
        load_fundamentals()

def on_message(ws_app, message):
    try:
        data = json.loads(message)
        if data.get('type') == 'trade':
            for trade in data.get('data', []):
                sym = trade.get('s','')
                price = trade.get('p')
                if sym and price and sym in ALL_TICKERS:
                    with _lock:
                        prev = _prices.get(sym,{}).get('prev_close')
                        chg  = round(price-prev,2) if prev else None
                        chgp = round((chg/prev)*100,2) if chg and prev else None
                        _prices[sym] = {**_prices.get(sym,{}),
                            'price':round(price,2),'change':chg,
                            'changepct':chgp,'source':'live'}
    except Exception as e: print(f"WS: {e}")

def on_error(ws_app,e): print(f"WS error: {e}")
def on_close(ws_app,c,m):
    # FIX: no llamar start_ws() aquí — causaría stack overflow por recursión infinita
    print(f"[{time.strftime('%H:%M:%S')}] WS cerrado. Loop de reconexión retomará en 5s.")
def on_open(ws_app):
    print(f"[{time.strftime('%H:%M:%S')}] WS conectado, suscribiendo {len(ALL_TICKERS)} tickers...")
    for sym in ALL_TICKERS:
        ws_app.send(json.dumps({"type":"subscribe","symbol":sym}))
    print("Suscripciones activas ✓")

def start_ws():
    """Loop de reconexión — nunca retorna. Reconecta con backoff exponencial."""
    _delay = 5
    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] Iniciando WebSocket Finnhub...")
            ws_app = websocket.WebSocketApp(f"wss://ws.finnhub.io?token={API_KEY}",
                on_message=on_message,on_error=on_error,on_close=on_close,on_open=on_open)
            ws_app.run_forever(ping_interval=30,ping_timeout=10)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] WS excepción: {e}")
        print(f"[{time.strftime('%H:%M:%S')}] WS desconectado. Reconectando en {_delay:.0f}s...")
        time.sleep(_delay)
        _delay = min(_delay * 1.5, 60)

app = Flask(__name__)

@app.route('/api/stocks')
def api_stocks():
    with _lock:
        result = []
        for ticker in ALL_TICKERS:
            p = _prices.get(ticker,{})
            i = _info.get(ticker,{})
            price = p.get('price')
            w52h  = i.get('w52h')
            tgt   = i.get('price_target')
            pct52   = round(((price-w52h)/w52h)*100,1) if price and w52h else None
            upside  = round(((tgt-price)/price)*100,1) if tgt and price else None
            result.append({
                'ticker':        ticker,
                'group':         'requested' if ticker in REQUESTED else ('recommended' if ticker in RECOMMENDED else 'ai'),
                'name':          i.get('name',ticker),
                'price':         price,
                'chg':           p.get('change'),
                'chgp':          p.get('changepct'),
                'currency':      i.get('currency','USD'),
                'mktcap_b':      i.get('mktcap_b'),
                'sector':        i.get('sector','—'),
                'pe_trail':      i.get('pe_trail'),
                'pe_fwd':        i.get('pe_fwd'),
                'pb':            i.get('pb'),
                'ps':            i.get('ps'),
                'eps':           i.get('eps'),
                'eps_growth':    i.get('eps_growth'),
                'rev_growth':    i.get('rev_growth'),
                'gross_margin':  i.get('gross_margin'),
                'net_margin':    i.get('net_margin'),
                'roe':           i.get('roe'),
                'roa':           i.get('roa'),
                'debt_eq':       i.get('debt_eq'),
                'beta':          i.get('beta'),
                'div_yield':     i.get('div_yield'),
                'w52h':          w52h,
                'w52l':          i.get('w52l'),
                'pct_from_52h':  pct52,
                'high_day':      p.get('high_day'),
                'low_day':       p.get('low_day'),
                'volume':        p.get('volume'),
                'price_target':  tgt,
                'upside':        upside,
                'analyst_buy':   i.get('analyst_buy'),
                'analyst_hold':  i.get('analyst_hold'),
                'analyst_sell':  i.get('analyst_sell'),
                'analyst_score': i.get('analyst_score'),
                'source':        p.get('source','—'),
                'rsi_d':         _tech.get(ticker,{}).get('rsi_d'),
                'rsi_w':         _tech.get(ticker,{}).get('rsi_w'),
                'ema200':        _tech.get(ticker,{}).get('ema200'),
                'ema50':         _tech.get(ticker,{}).get('ema50'),
                'pct_e200':      _tech.get(ticker,{}).get('pct_e200'),
                'golden':        _tech.get(ticker,{}).get('golden'),
                'macd_bull':     _tech.get(ticker,{}).get('macd_bull'),
                'macd_hist':     _tech.get(ticker,{}).get('macd_hist'),
                'tech_signal':   _tech.get(ticker,{}).get('signal'),
                'atr14':         _tech.get(ticker,{}).get('atr14'),
                'rvol':          i.get('rvol'),
                'avg_volume':    i.get('avg_volume'),
                'sparkline':     _sparklines.get(ticker, []),
            })
    return jsonify({'data':result,'updated_at':time.strftime("%d/%m/%Y  %H:%M:%S")})

@app.route('/api/market')
def api_market():
    with _lock:
        return jsonify(dict(_market))

@app.route('/api/mining')
def api_mining():
    with _lock:
        return jsonify(dict(_mining))

@app.route('/api/options')
def api_options():
    with _lock:
        return jsonify(dict(_options))

@app.route('/api/news/<ticker>')
def api_news(ticker):
    if ticker not in ALL_TICKERS:
        return jsonify({'error':'ticker no válido'})
    try:
        now  = datetime.now()
        news = fc.company_news(ticker,
            _from=(now - timedelta(days=7)).strftime('%Y-%m-%d'),
            to=now.strftime('%Y-%m-%d'))
        result = []
        for n in (news or [])[:10]:
            ts = n.get('datetime',0)
            try:
                dt = datetime.fromtimestamp(ts)
                diff = now - dt
                if diff.days > 0:    ago = f"hace {diff.days}d"
                elif diff.seconds > 3600: ago = f"hace {diff.seconds//3600}h"
                else:                ago = f"hace {diff.seconds//60}m"
            except Exception:
                ago = '—'
            result.append({
                'headline': n.get('headline',''),
                'source':   n.get('source',''),
                'url':      n.get('url','#'),
                'image':    n.get('image',''),
                'summary':  (n.get('summary','') or '')[:220],
                'ago':      ago,
            })
        return jsonify({'ticker':ticker,'news':result})
    except Exception as e:
        return jsonify({'error':str(e),'news':[]})

@app.route('/')
def index():
    return render_template_string(HTML, poll=POLL_MS)

HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>USA Stock Tracker — Live</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a1520;color:#dce8f0;font-family:'Segoe UI',Arial,sans-serif;min-height:100vh}

/* header */
header{background:linear-gradient(135deg,#142030,#0a1520);padding:13px 20px;
  border-bottom:2px solid #1e3a56;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
h1{font-size:1.28rem;color:#f0c040;font-weight:700;letter-spacing:.3px}
h1 span{color:#6a9aba;font-size:.82rem;font-weight:400;margin-left:10px}
#meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap}

/* badges */
.badge{border-radius:10px;padding:3px 10px;font-size:.72rem;font-weight:700;letter-spacing:.4px}
.mkt-open {background:#0a2a18;color:#2ecc71;border:1px solid #1a5a30}
.mkt-pre  {background:#182510;color:#90c040;border:1px solid #305010}
.mkt-after{background:#2a1a06;color:#e09030;border:1px solid #603808}
.mkt-closed{background:#141420;color:#506070;border:1px solid #202030}
.live-badge{background:#0a2218;color:#2ecc71;border:1px solid #1a5228;border-radius:10px;padding:3px 10px;font-size:.72rem;font-weight:700}
#updated{font-size:.75rem;color:#405060}

/* toolbar */
.toolbar{display:flex;gap:8px;padding:9px 18px;background:#0d1a28;border-bottom:1px solid #162636;align-items:center;flex-wrap:wrap}
#search{background:#0a1520;border:1px solid #1e3a56;color:#dce8f0;border-radius:6px;padding:5px 11px;font-size:.8rem;width:200px;outline:none}
#search:focus{border-color:#2a6aaa}
#search::placeholder{color:#304050}
.fbtn{background:#0a1520;border:1px solid #1e3a56;color:#6a9aba;border-radius:5px;padding:4px 11px;font-size:.76rem;cursor:pointer;transition:all .15s}
.fbtn:hover,.fbtn.active{background:#142a42;color:#dce8f0;border-color:#2a6aaa}
.alert-ctrl{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:.75rem;color:#405060}
.alert-ctrl input{width:50px;background:#0a1520;border:1px solid #1e3a56;color:#dce8f0;border-radius:4px;padding:3px 6px;font-size:.76rem;text-align:center}

/* tabs */
.tabs{display:flex;gap:0;padding:0 18px;background:#0c1825;border-bottom:2px solid #162636}
.tab{padding:9px 18px;font-size:.8rem;font-weight:600;color:#405878;cursor:pointer;
  border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .2s;letter-spacing:.3px}
.tab:hover{color:#8abada}
.tab.active{color:#f0c040;border-bottom-color:#f0c040}

/* top pick */
.pick{display:inline-flex;align-items:center;justify-content:center;
  width:18px;height:18px;border-radius:50%;background:#0a2248;
  color:#4a9eff;font-size:.75rem;font-weight:900;border:1.5px solid #2a5aaa;
  cursor:help;flex-shrink:0}
.pick-row td:nth-child(1){background:#0a1e36 !important}
.score-pill{display:inline-block;background:#0a1e36;color:#4a9eff;border:1px solid #1a3a6a;
  border-radius:8px;padding:1px 7px;font-size:.7rem;font-weight:700}

/* legend */
.legend{display:flex;gap:14px;padding:5px 18px;background:#0a1520;font-size:.7rem;
  color:#304050;flex-wrap:wrap;align-items:center;border-bottom:1px solid #0e1e2e}
.leg-item{display:flex;align-items:center;gap:5px}
.leg-dot{width:8px;height:8px;border-radius:2px}
.leg-right{margin-left:auto;font-size:.67rem;color:#1e3040}

/* table */
.section-title{padding:9px 18px 3px;font-size:.68rem;font-weight:700;
  letter-spacing:2px;text-transform:uppercase;color:#4a7890}
.table-wrap{padding:0 8px 6px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.78rem}
thead tr{background:#10222e;position:sticky;top:0;z-index:10}
thead th{padding:7px 9px;text-align:right;color:#688aaa;font-weight:600;font-size:.7rem;
  letter-spacing:.3px;border-bottom:1px solid #162636;white-space:nowrap;
  cursor:pointer;user-select:none;transition:color .15s}
thead th:hover{color:#a8cce8}
thead th.sort-asc::after{content:' ▲';color:#f0c040;font-size:.62rem}
thead th.sort-desc::after{content:' ▼';color:#f0c040;font-size:.62rem}
thead th.no-sort{cursor:default}

tbody tr{border-bottom:1px solid #0e1e2a;transition:background .1s}
tbody tr:hover{background:#10202e}
tbody tr.req  td:first-child{border-left:3px solid #2a6aaa}
tbody tr.rec  td:first-child{border-left:3px solid #20a060}
tbody tr.ai   td:first-child{border-left:3px solid #9040c0}
tbody tr.alert-hi{background:#082018 !important}
tbody tr.alert-lo{background:#200808 !important}

td{padding:6px 9px;text-align:right;vertical-align:middle;white-space:nowrap}
td.left{text-align:left}

.ticker{font-weight:700;color:#60c0e0;font-size:.84rem}
.tname{color:#607888;font-size:.72rem;max-width:170px;overflow:hidden;text-overflow:ellipsis;display:block}
.price{font-weight:700;color:#dce8f0;font-size:.88rem}
.pos{color:#2ecc71}.neg{color:#e74c3c}.neu{color:#506070}
.muted{color:#304050}
.pos-bg{background:#0a2010;color:#2ecc71;border-radius:3px;padding:0 4px}
.neg-bg{background:#200808;color:#e74c3c;border-radius:3px;padding:0 4px}
.na{color:#203040}

/* sector tag */
.stag{display:inline-block;background:#101e2e;color:#507090;border-radius:3px;
  padding:1px 5px;font-size:.66rem;border:1px solid #1a2e40;max-width:130px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* analyst bar */
.abar{display:flex;height:6px;border-radius:3px;overflow:hidden;min-width:60px;gap:1px}
.abar-buy{background:#2ecc71}.abar-hold{background:#f0c040}.abar-sell{background:#e74c3c}
.ascore{font-weight:700;font-size:.8rem;margin-right:5px}
.score-5{color:#2ecc71}.score-4{color:#90d050}.score-3{color:#f0c040}
.score-2{color:#e09040}.score-1{color:#e74c3c}

/* source dots */
.src-live{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:#2ecc71;animation:blink 1.8s infinite;margin-right:3px}
.src-rest{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:#2a5aaa;margin-right:3px}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.1}}

.flash-up{animation:fup .5s ease}
.flash-dn{animation:fdn .5s ease}
@keyframes fup{0%{background:#082818}100%{background:transparent}}
@keyframes fdn{0%{background:#280808}100%{background:transparent}}

footer{padding:8px 18px;text-align:center;color:#1e3040;font-size:.68rem;border-top:1px solid #0e1e2e;margin-top:4px}
::-webkit-scrollbar{height:4px;width:4px}
::-webkit-scrollbar-track{background:#0a1520}
::-webkit-scrollbar-thumb{background:#1e3a56;border-radius:3px}

/* sparklines */
.spark{display:inline-block;vertical-align:middle;cursor:pointer}
.spark:hover{opacity:.75}

/* news panel */
#news-panel{position:fixed;top:0;right:-440px;width:420px;height:100vh;
  background:#0c1825;border-left:2px solid #1e3a56;z-index:1000;
  transition:right .3s ease;display:flex;flex-direction:column;box-shadow:-8px 0 30px rgba(0,0,0,.5)}
#news-panel.open{right:0}
.news-hdr{display:flex;align-items:center;justify-content:space-between;
  padding:14px 18px;background:#0a1520;border-bottom:1px solid #1e3a56;flex-shrink:0}
.news-hdr-ticker{font-size:1.1rem;font-weight:700;color:#f0c040}
.news-hdr-name{font-size:.78rem;color:#6a9aba;margin-top:2px}
#news-close{background:none;border:1px solid #2a4a6a;color:#6a9aba;border-radius:5px;
  padding:4px 10px;cursor:pointer;font-size:.85rem;transition:all .15s}
#news-close:hover{background:#1a3a5a;color:#dce8f0}
#news-list{overflow-y:auto;flex:1;padding:10px 14px}
.news-item{padding:11px 0;border-bottom:1px solid #0e1e2e}
.news-item:last-child{border-bottom:none}
.news-headline{font-size:.82rem;font-weight:600;color:#b0cce0;line-height:1.4;
  text-decoration:none;display:block;margin-bottom:5px}
.news-headline:hover{color:#f0c040}
.news-meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.news-source{font-size:.69rem;color:#2a6aaa;font-weight:600;background:#0a1e36;
  padding:1px 6px;border-radius:3px}
.news-ago{font-size:.69rem;color:#304050}
.news-summary{font-size:.74rem;color:#506878;line-height:1.45;margin-top:5px}
.news-img{width:100%;border-radius:5px;margin-bottom:6px;max-height:100px;object-fit:cover;opacity:.85}
.news-loading{text-align:center;padding:40px;color:#304050;font-size:.85rem}
#news-overlay{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:999;display:none}
#news-overlay.open{display:block}

/* technical indicators */
.rsi-ob  {background:#2a0a0a;color:#e74c3c;border:1px solid #5a1a1a;border-radius:5px;padding:1px 7px;font-weight:700;font-size:.75rem}
.rsi-os  {background:#0a2a10;color:#2ecc71;border:1px solid #1a5a20;border-radius:5px;padding:1px 7px;font-weight:700;font-size:.75rem}
.rsi-neu {background:#101e2e;color:#7a9aba;border:1px solid #1e3040;border-radius:5px;padding:1px 7px;font-weight:700;font-size:.75rem}
.rsi-warn{background:#2a1e08;color:#e0a030;border:1px solid #5a3a08;border-radius:5px;padding:1px 7px;font-weight:700;font-size:.75rem}
.ema-above{color:#2ecc71;font-weight:600}
.ema-below{color:#e74c3c;font-weight:600}
.ema-near {color:#f0c040;font-weight:600}
.cross-gold {background:#1a2a08;color:#d4ac00;border:1px solid #4a3a00;border-radius:5px;padding:1px 7px;font-size:.72rem;font-weight:700}
.cross-death{background:#1a0808;color:#e05050;border:1px solid #4a1010;border-radius:5px;padding:1px 7px;font-size:.72rem;font-weight:700}
.macd-bull{color:#2ecc71;font-weight:600}.macd-bear{color:#e74c3c;font-weight:600}
.sig-buy    {background:#0a2818;color:#2ecc71;border:1px solid #1a5828;border-radius:6px;padding:2px 9px;font-weight:700;font-size:.76rem;letter-spacing:.5px}
.sig-sell   {background:#280a0a;color:#e74c3c;border:1px solid #581a1a;border-radius:6px;padding:2px 9px;font-weight:700;font-size:.76rem;letter-spacing:.5px}
.sig-neutral{background:#101e2e;color:#7a9aba;border:1px solid #1e3040;border-radius:6px;padding:2px 9px;font-weight:700;font-size:.76rem;letter-spacing:.5px}

/* ── Opciones ────────────────────────────────────────────────────── */
.opt-wrap{padding:10px 18px 28px}
.opt-ticker-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin-bottom:6px}
.opt-card{background:#0d1a28;border:1px solid #162636;border-radius:8px;padding:14px 16px;min-width:0;overflow:hidden}
.opt-tbl-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:10px}
.opt-card-hdr{display:flex;align-items:center;gap:10px;margin-bottom:10px;border-bottom:1px solid #0e2030;padding-bottom:8px;flex-wrap:wrap}
.opt-sym{font-size:1.1rem;font-weight:800;color:#90b8d8}
.opt-name{font-size:.72rem;color:#304050}
.opt-price-big{font-size:1.1rem;font-weight:700;color:#c8d8e8;font-family:monospace;margin-left:auto}
.opt-pc-chip{font-size:.72rem;font-weight:700;padding:2px 8px;border-radius:4px;margin-left:6px}
.opt-pc-bull{background:#0a2010;color:#2ecc71;border:1px solid #1a4020}
.opt-pc-bear{background:#200a0a;color:#e74c3c;border:1px solid #401a1a}
.opt-pc-neu {background:#101e2e;color:#a0b8c8;border:1px solid #1e3040}
.opt-section-lbl{font-size:.63rem;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#2a6aaa;margin:8px 0 4px}
.opt-table{width:100%;border-collapse:collapse;font-size:.73rem;margin-bottom:0}
.opt-table th{color:#304050;font-size:.60rem;font-weight:600;letter-spacing:.3px;text-transform:uppercase;padding:4px 5px;border-bottom:1px solid #0e2030;white-space:nowrap}
.opt-table th.r{text-align:right}.opt-table td{padding:3px 5px;border-bottom:1px solid #091520;white-space:nowrap;vertical-align:middle}
.opt-table td.r{text-align:right}
.opt-table tr:hover td{background:#0d1d2c}
.exp-date{font-weight:600;color:#a0b8c8}
.exp-badge{font-size:.58rem;padding:1px 5px;border-radius:3px;font-weight:700;margin-left:3px;vertical-align:middle}
.exp-q{background:#1a1005;color:#f0a020;border:1px solid #2a2010}
.exp-m{background:#0a1020;color:#6090c0;border:1px solid #162030}
.exp-dte{color:#405060;font-size:.68rem}
.oi-call{color:#2ecc71}.oi-put{color:#e74c3c}.oi-tot{color:#90a8b8;font-size:.68rem}
.mp-price{font-weight:700;color:#f0c040;font-family:monospace}
.mp-dist-neg{color:#e74c3c;font-size:.70rem}.mp-dist-pos{color:#2ecc71;font-size:.70rem}.mp-dist-neu{color:#607080;font-size:.70rem}
.gamma-bar-wrap{width:100%;height:28px;background:#050f18;border-radius:4px;position:relative;overflow:hidden;margin:4px 0}
.gamma-call-seg,.gamma-put-seg{position:absolute;top:0;height:100%;opacity:.85}
.gamma-call-seg{background:#1a4a20;left:50%}
.gamma-put-seg {background:#4a1010;right:50%}
.gamma-lbl{position:absolute;top:50%;transform:translateY(-50%);font-size:.60rem;pointer-events:none;padding:0 4px;white-space:nowrap}
.gamma-center-line{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#1e3a50;z-index:2}
.opt-insight{background:#060f18;border:1px solid #0e2030;border-radius:6px;padding:8px 10px;font-size:.70rem;color:#4a7090;line-height:1.5;margin-top:6px}
.opt-insight b{color:#608090}
.oi-bar-cell{min-width:80px}
.oi-mini{display:inline-block;height:8px;border-radius:2px;vertical-align:middle;margin-right:3px}

/* ── Materias Primas ─────────────────────────────────────────────── */
.mining-wrap{padding:10px 18px 24px}
.mining-section{margin-bottom:18px}
.mining-section-title{font-size:.72rem;font-weight:700;letter-spacing:1.5px;color:#2a6aaa;text-transform:uppercase;padding:6px 0 4px;border-bottom:1px solid #0e2030;margin-bottom:4px}
.mining-table{width:100%;border-collapse:collapse;font-size:.78rem}
.mining-table th{color:#304a60;font-size:.65rem;font-weight:600;letter-spacing:.5px;text-transform:uppercase;padding:5px 8px;border-bottom:1px solid #0e2030;white-space:nowrap}
.mining-table th.r{text-align:right}
.mining-table td{padding:5px 8px;border-bottom:1px solid #091520;white-space:nowrap;vertical-align:middle}
.mining-table tr:hover td{background:#0d1d2c}
.mining-table td.r{text-align:right}
.m-ticker{font-weight:700;color:#90b8d0;font-size:.82rem}
.m-name{color:#304050;font-size:.68rem}
.m-price{font-weight:600;color:#c8d8e8;font-family:monospace}
.m-chg-pos{color:#2ecc71;font-size:.76rem}
.m-chg-neg{color:#e74c3c;font-size:.76rem}
.m-badge{font-size:.60rem;padding:1px 5px;border-radius:3px;font-weight:700;margin-left:3px}
.m-badge-gold{background:#2a1e05;color:#f0c040;border:1px solid #3a2e10}
.m-badge-silver{background:#151e2a;color:#a0b8d0;border:1px solid #253040}
.m-badge-copper{background:#1e1205;color:#e08050;border:1px solid #2e2010}
.m-badge-uranium{background:#0a1a0a;color:#50d050;border:1px solid #103010}
.m-badge-miner{background:#0a1420;color:#6090b0;border:1px solid #1a2a3a}
.m-badge-div{background:#100f20;color:#9080c0;border:1px solid #201f30}
.range30-bar{display:inline-block;width:60px;height:6px;background:#0a1520;border-radius:3px;vertical-align:middle;position:relative;overflow:hidden}
.range30-fill{position:absolute;top:0;left:0;height:100%;border-radius:3px;background:linear-gradient(90deg,#e74c3c,#f0c040,#2ecc71)}
.rsi-ob{color:#e74c3c;font-weight:700}
.rsi-os{color:#2ecc71;font-weight:700}
.rsi-mid{color:#a0b8c8}
.d-neg{color:#e74c3c}.d-pos{color:#2ecc71}.d-neu{color:#7a9aba}
.vol-fmt{color:#405060;font-size:.72rem}

/* ── Resumen Diario ─────────────────────────────────────────────── */
.mkt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;padding:16px 18px}
.mkt-card{background:#0d1a28;border:1px solid #162636;border-radius:8px;padding:14px 16px}
.mkt-card-title{font-size:.68rem;font-weight:700;letter-spacing:2px;text-transform:uppercase;
  color:#4a7890;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.mkt-card-title span{font-size:.75rem}
.mkt-inst-row{display:flex;justify-content:space-between;align-items:center;
  padding:5px 0;border-bottom:1px solid #0e1e2a}
.mkt-inst-row:last-child{border-bottom:none}
.mkt-inst-sym{font-weight:700;font-size:.82rem;color:#60c0e0;min-width:60px}
.mkt-inst-name{font-size:.7rem;color:#405060;flex:1;padding:0 8px}
.mkt-inst-price{font-weight:700;font-size:.88rem;color:#dce8f0;min-width:70px;text-align:right}
.mkt-inst-chg{font-size:.78rem;min-width:65px;text-align:right;font-weight:600}
.bias-card{background:#0d1a28;border:1px solid #162636;border-radius:8px;padding:14px 16px}
.bias-row{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #0e1e2a}
.bias-row:last-child{border-bottom:none}
.bias-sym{font-weight:700;color:#60c0e0;font-size:.84rem;min-width:55px}
.bias-label{font-size:.72rem;color:#405060;min-width:50px}
.bias-long  {background:#0a2818;color:#2ecc71;border:1px solid #1a5828;border-radius:6px;padding:2px 10px;font-weight:700;font-size:.76rem}
.bias-short {background:#280a0a;color:#e74c3c;border:1px solid #581a1a;border-radius:6px;padding:2px 10px;font-weight:700;font-size:.76rem}
.bias-neutral{background:#101e2e;color:#7a9aba;border:1px solid #1e3040;border-radius:6px;padding:2px 10px;font-weight:700;font-size:.76rem}
.pivot-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-top:6px}
.pivot-item{text-align:center;padding:4px 0}
.pivot-item .plabel{font-size:.62rem;color:#304050}
.pivot-item .pval  {font-size:.78rem;font-weight:700}
.pivot-item.res .pval{color:#e74c3c}.pivot-item.sup .pval{color:#2ecc71}.pivot-item.pvt .pval{color:#f0c040}
.movers-table{width:100%;border-collapse:collapse}
.movers-table td{padding:4px 6px;font-size:.76rem;border-bottom:1px solid #0e1e2a}
.movers-table tr:last-child td{border-bottom:none}
.movers-ticker{font-weight:700;color:#60c0e0}
.news-mkt-item{padding:8px 0;border-bottom:1px solid #0e1e2a}
.news-mkt-item:last-child{border-bottom:none}
.news-mkt-hl{font-size:.78rem;font-weight:600;color:#b0cce0;text-decoration:none;display:block;line-height:1.4;margin-bottom:3px}
.news-mkt-hl:hover{color:#f0c040}
.news-mkt-meta{font-size:.67rem;color:#304050}
.mkt-loading{text-align:center;padding:60px;color:#304050;font-size:.9rem}

/* RVOL */
.rvol-hot  {background:#2a1a00;color:#f0a030;border:1px solid #5a3a00;border-radius:4px;padding:1px 6px;font-weight:700;font-size:.74rem}
.rvol-high {background:#1a2a00;color:#90d050;border:1px solid #3a5a00;border-radius:4px;padding:1px 6px;font-weight:700;font-size:.74rem}
.rvol-norm {background:#101e2e;color:#608090;border:1px solid #1e3040;border-radius:4px;padding:1px 6px;font-size:.74rem}
.rvol-low  {color:#304050;font-size:.74rem}

/* Fear & Greed */
.fg-meter{display:flex;flex-direction:column;align-items:center;padding:8px 0}
.fg-score{font-size:2.8rem;font-weight:900;line-height:1}
.fg-label{font-size:.78rem;font-weight:700;letter-spacing:1px;margin-top:4px}
.fg-bar-wrap{width:100%;background:#0a1520;border-radius:4px;height:8px;margin-top:8px;overflow:hidden}
.fg-bar{height:100%;border-radius:4px;transition:width .5s}
.fg-greed{color:#2ecc71}.fg-fgreed{color:#90d050}
.fg-neutral{color:#f0c040}
.fg-fear{color:#e09040}.fg-xfear{color:#e74c3c}

/* Sector heatmap */
.sector-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:6px}
.sector-cell{border-radius:6px;padding:8px 10px;text-align:center}
.sector-name{font-size:.67rem;color:rgba(255,255,255,.6);margin-bottom:3px}
.sector-sym{font-size:.7rem;font-weight:700;color:rgba(255,255,255,.4);margin-bottom:2px}
.sector-chg{font-size:.88rem;font-weight:700}

/* Economic Calendar */
.cal-row{display:grid;grid-template-columns:75px 1fr 55px 65px 65px;gap:6px;
  align-items:center;padding:6px 0;border-bottom:1px solid #0e1e2e;font-size:.75rem}
.cal-row:last-child{border-bottom:none}
.cal-time{color:#405060;font-size:.72rem}
.cal-event{color:#b0cce0;font-weight:600}
.cal-imp-high{color:#e74c3c;font-weight:700;font-size:.68rem}
.cal-imp-medium{color:#f0c040;font-size:.68rem}
.cal-imp-low{color:#405060;font-size:.68rem}
.cal-val{text-align:right;font-size:.72rem}
.cal-act{color:#2ecc71;font-weight:700}

/* Earnings */
.earn-row{display:grid;grid-template-columns:55px 75px 45px 85px 85px;gap:6px;
  align-items:center;padding:5px 0;border-bottom:1px solid #0e1e2e;font-size:.74rem}
.earn-row:last-child{border-bottom:none}
.earn-ticker{font-weight:700;color:#60c0e0}
.earn-date{color:#405060;font-size:.72rem}
.earn-hour{font-size:.67rem;padding:1px 5px;border-radius:3px}
.earn-bmo{background:#0a1e36;color:#4a9eff}
.earn-amc{background:#1a0a2e;color:#9060e0}
.earn-dmh{background:#101e2e;color:#607080}

/* Pre-market movers */
.pre-row{display:flex;justify-content:space-between;align-items:center;
  padding:4px 0;border-bottom:1px solid #0e1e2a;font-size:.76rem}
.pre-row:last-child{border-bottom:none}
.pre-ticker{font-weight:700;color:#60c0e0;min-width:50px}

/* Breadth */
.breadth-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.breadth-item{background:#0a1520;border-radius:6px;padding:8px 10px;text-align:center}
.breadth-label{font-size:.64rem;color:#304050;margin-bottom:3px}
.breadth-val{font-size:1.1rem;font-weight:700}
</style>
</head>
<body>

<header>
  <h1>🇺🇸 USA Stock Tracker <span>Finnhub Real-Time</span></h1>
  <div id="meta">
    <div id="mkt-status" class="badge mkt-closed">● CERRADO</div>
    <div id="live-count" class="live-badge" style="display:none"></div>
    <div id="updated">Conectando...</div>
  </div>
</header>

<div class="toolbar">
  <input id="search" type="text" placeholder="🔍 Buscar ticker o empresa..." oninput="renderAll()">
  <div style="display:flex;gap:5px">
    <button class="fbtn active" onclick="setGroup('all',this)">Todas</button>
    <button class="fbtn" onclick="setGroup('requested',this)">Solicitadas</button>
    <button class="fbtn" onclick="setGroup('recommended',this)">Recomendadas</button>
    <button class="fbtn" onclick="setGroup('ai',this)">🚀 IA / Growth</button>
  </div>
  <div class="alert-ctrl">⚡ Alerta &gt; <input id="thr" type="number" value="3" min="0.5" max="20" step="0.5"> %</div>
</div>

<div class="tabs">
  <div class="tab active" onclick="setTab('prices',this)">💰 Precios</div>
  <div class="tab" onclick="setTab('valuation',this)">📊 Valuación</div>
  <div class="tab" onclick="setTab('growth',this)">📈 Crecimiento</div>
  <div class="tab" onclick="setTab('analysts',this)">🎯 Analistas</div>
  <div class="tab" onclick="setTab('technical',this)">📉 Técnico</div>
  <div class="tab" onclick="setTab('market',this)">🌍 Resumen Diario</div>
  <div class="tab" onclick="setTab('mining',this)">⛏️ Materias Primas</div>
  <div class="tab" onclick="setTab('options',this)">📋 Opciones</div>
</div>

<div class="legend">
  <div class="leg-item"><div class="leg-dot" style="background:#2a6aaa"></div>Solicitadas</div>
  <div class="leg-item"><div class="leg-dot" style="background:#20a060"></div>Recomendadas</div>
  <div class="leg-item"><div class="leg-dot" style="background:#9040c0"></div>IA / Growth</div>
  <div class="leg-item"><span class="src-live"></span>Vivo (WS)</div>
  <div class="leg-item"><span class="src-rest"></span>REST</div>
  <div class="leg-item"><span class="pick" style="cursor:default">✦</span>&nbsp;Top Pick — mejor combinación de crecimiento + valuación según analistas</div>
  <div class="leg-right">Click en columna para ordenar · Actualización cada 3s · Finnhub.io</div>
</div>

<div id="root"><div style="text-align:center;padding:60px;color:#304050">⏳ Cargando...</div></div>
<footer>Datos de Finnhub.io · yfinance. Fuera de horario de mercado los precios son del último cierre. No constituye consejo de inversión.</footer>

<!-- news panel -->
<div id="news-overlay" onclick="closeNews()"></div>
<div id="news-panel">
  <div class="news-hdr">
    <div>
      <div class="news-hdr-ticker" id="news-ticker-label">—</div>
      <div class="news-hdr-name"   id="news-name-label">Noticias · últimos 7 días</div>
    </div>
    <button id="news-close" onclick="closeNews()">✕ Cerrar</button>
  </div>
  <div id="news-list"><div class="news-loading">Seleccioná un ticker para ver noticias</div></div>
</div>

<script>
const POLL = {{ poll }};
let allData=[], sortCol=null, sortDir=1, activeGroup='all', activeTab='prices', prevPrices={};

/* ── market status ── */
function mktStatus(){
  const et=new Date(new Date().toLocaleString('en-US',{timeZone:'America/New_York'}));
  const d=et.getDay(),m=et.getHours()*60+et.getMinutes();
  if(d===0||d===6)return{cls:'mkt-closed',lbl:'● FIN DE SEMANA'};
  if(m>=570&&m<960) return{cls:'mkt-open', lbl:'● MERCADO ABIERTO'};
  if(m>=240&&m<570) return{cls:'mkt-pre',  lbl:'● PRE-MERCADO'};
  if(m>=960&&m<1200)return{cls:'mkt-after',lbl:'● AFTER-HOURS'};
  return{cls:'mkt-closed',lbl:'● CERRADO'};
}
function updateMkt(){const{cls,lbl}=mktStatus();const el=document.getElementById('mkt-status');el.textContent=lbl;el.className='badge '+cls;}
updateMkt(); setInterval(updateMkt,30000);

/* ── helpers ── */
function f(v,dec=2,pre='',suf=''){
  if(v==null||isNaN(v))return'<span class="na">—</span>';
  return pre+Number(v).toFixed(dec)+suf;
}
function fPct(v,dec=1){
  if(v==null||isNaN(v))return'<span class="na">—</span>';
  const cls2=v>=0?'pos':'neg';
  return`<span class="${cls2}">${v>=0?'+':''}${Number(v).toFixed(dec)}%</span>`;
}
function fCap(v){
  if(v==null)return'<span class="na">—</span>';
  if(v>=1000)return'$'+(v/1000).toFixed(2)+'T';
  return'$'+v.toFixed(1)+'B';
}
function fVol(v){
  if(!v)return'<span class="na">—</span>';
  if(v>=1e9)return(v/1e9).toFixed(2)+'B';
  if(v>=1e6)return(v/1e6).toFixed(1)+'M';
  if(v>=1e3)return(v/1e3).toFixed(0)+'K';
  return v;
}
function cls(v){return v==null?'neu':v>=0?'pos':'neg';}
function arr(v){return v==null?'':v>=0?'▲ ':'▼ ';}
function src(s){return s==='live'?'<span class="src-live"></span>':'<span class="src-rest"></span>';}

/* ── analyst score ── */
function analystScore(s){
  if(s==null)return'<span class="na">—</span>';
  const scls=['','score-1','score-2','score-3','score-4','score-5'][Math.round(s)]||'score-3';
  const labels=['','Sell','U-weight','Hold','O-weight','Buy'];
  return`<span class="ascore ${scls}">${s.toFixed(1)}</span><small style="color:#405060">${labels[Math.round(s)]||''}</small>`;
}
function analystBar(buy,hold,sell){
  if(buy==null)return'<span class="na">—</span>';
  const total=buy+hold+sell||1;
  const bPct=buy/total*100,hPct=hold/total*100,sPct=sell/total*100;
  return`<div style="display:flex;align-items:center;gap:5px">
    <div class="abar" style="flex:1">
      <div class="abar-buy" style="width:${bPct}%"></div>
      <div class="abar-hold" style="width:${hPct}%"></div>
      <div class="abar-sell" style="width:${sPct}%"></div>
    </div>
    <small style="color:#405060;font-size:.67rem">${buy}/${hold}/${sell}</small>
  </div>`;
}

/* ── technical helpers ── */
function rsiCell(v){
  if(v==null) return '<span class="na">—</span>';
  const rv=v.toFixed(1);
  if(v>70) return`<span class="rsi-ob">🔴 ${rv} Sobrecompra</span>`;
  if(v>60) return`<span class="rsi-warn">🟡 ${rv} Alto</span>`;
  if(v<30) return`<span class="rsi-os">🟢 ${rv} Sobreventa</span>`;
  if(v<40) return`<span class="rsi-warn">🟡 ${rv} Bajo</span>`;
  return`<span class="rsi-neu">⚪ ${rv} Neutro</span>`;
}
function emaCell(pct){
  if(pct==null) return '<span class="na">—</span>';
  const near = Math.abs(pct)<3;
  const p = (pct>=0?'+':'')+pct.toFixed(1)+'%';
  if(near)       return`<span class="ema-near">≈ EMA200 (${p})</span>`;
  if(pct>0)      return`<span class="ema-above">↑ ${p} sobre EMA200</span>`;
  return`<span class="ema-below">↓ ${p} bajo EMA200</span>`;
}
function crossCell(golden){
  if(golden==null) return '<span class="na">—</span>';
  return golden
    ? '<span class="cross-gold">✦ Golden Cross</span>'
    : '<span class="cross-death">✕ Death Cross</span>';
}
function macdCell(bull, hist){
  if(bull==null) return '<span class="na">—</span>';
  const h = hist!=null?` (${hist>0?'+':''}${hist.toFixed(2)})`:'';
  return bull
    ? `<span class="macd-bull">▲ Alcista${h}</span>`
    : `<span class="macd-bear">▼ Bajista${h}</span>`;
}
function sigCell(sig){
  if(!sig) return '<span class="na">—</span>';
  if(sig==='buy')     return'<span class="sig-buy">▲ COMPRAR</span>';
  if(sig==='sell')    return'<span class="sig-sell">▼ EVITAR</span>';
  return'<span class="sig-neutral">— NEUTRAL</span>';
}

/* ── RVOL cell ── */
function rvolCell(v){
  if(v==null) return '<span class="na">—</span>';
  const rv = v.toFixed(2)+'x';
  if(v>=3)   return`<span class="rvol-hot">🔥 ${rv}</span>`;
  if(v>=1.5) return`<span class="rvol-high">⬆ ${rv}</span>`;
  if(v>=0.8) return`<span class="rvol-norm">${rv}</span>`;
  return`<span class="rvol-low">${rv}</span>`;
}

/* ── top pick scoring ── */
function computeScore(s){
  // Each component normalized 0–10
  let total=0, weight=0;

  // 1. Analyst consensus (40% weight) — score 1–5 → 0–10
  if(s.analyst_score!=null){
    total += ((s.analyst_score-1)/4)*10 * 4;
    weight += 4;
  }
  // 2. Upside vs price target (30% weight) — capped at 60%
  if(s.upside!=null){
    total += Math.min(Math.max(s.upside/60,0),1)*10 * 3;
    weight += 3;
  }
  // 3. Revenue growth YoY (15% weight) — capped at 60%
  if(s.rev_growth!=null){
    total += Math.min(Math.max(s.rev_growth*100/60,0),1)*10 * 1.5;
    weight += 1.5;
  }
  // 4. EPS growth YoY (10% weight) — capped at 80%
  if(s.eps_growth!=null){
    total += Math.min(Math.max(s.eps_growth*100/80,0),1)*10 * 1;
    weight += 1;
  }
  // 5. PEG bonus: low forward P/E relative to growth (5% weight)
  if(s.pe_fwd!=null && s.pe_fwd>0 && s.rev_growth!=null && s.rev_growth>0){
    const peg = s.pe_fwd / (s.rev_growth*100);
    total += Math.min(Math.max((3-peg)/3,0),1)*10 * 0.5;
    weight += 0.5;
  }

  if(weight===0) return null;
  return Math.round((total/weight)*10)/10;
}

function markTopPicks(data){
  const scored = data.map(s=>({...s, _score: computeScore(s)}))
                     .filter(s=>s._score!=null);
  // threshold: top 30% OR score >= 7.0
  scored.sort((a,b)=>b._score-a._score);
  const cutoff = Math.max(7.0, scored[Math.floor(scored.length*0.30)]?._score||0);
  const picks  = new Set(scored.filter(s=>s._score>=cutoff).map(s=>s.ticker));
  return data.map(s=>({
    ...s,
    _score: computeScore(s),
    _pick:  picks.has(s.ticker),
  }));
}

function pickCell(s){
  if(!s._pick) return '<td></td>';
  const reasons=[];
  if(s.analyst_score!=null) reasons.push(`Score analistas: ${s.analyst_score.toFixed(1)}/5`);
  if(s.upside!=null)        reasons.push(`Upside: +${s.upside.toFixed(1)}%`);
  if(s.rev_growth!=null)    reasons.push(`Rev growth: +${(s.rev_growth*100).toFixed(0)}%`);
  if(s.pe_fwd!=null)        reasons.push(`P/E fwd: ${s.pe_fwd.toFixed(1)}x`);
  if(s._score!=null)        reasons.push(`Score: ${s._score.toFixed(1)}/10`);
  const tip = reasons.join(' · ');
  return`<td><span class="pick" title="${tip}">✦</span></td>`;
}

/* ── tab columns ── */
const TABS = {
  prices: [
    {h:'',       sk:null,         r:(s)=>src(s.source),            left:true, pick:false},
    {h:'Pick',      sk:null,         r:(s)=>'__pick__',               left:true, pick:true},
    {h:'Ticker',    sk:'ticker',     r:(s)=>`<span class="ticker" style="cursor:pointer" onclick="openNews('${s.ticker}','${s.name.replace(/'/g,'')}')" title="Ver noticias">${s.ticker} 📰</span>`, left:true},
    {h:'Empresa',   sk:'name',       r:(s)=>`<span class="tname" title="${s.name}">${s.name}</span>`, left:true},
    {h:'7 días',    sk:null,         r:(s)=>sparklineSVG(s.sparkline),  left:true},
    {h:'Precio',    sk:'price',      r:(s)=>`<span class="price">${s.currency==='USD'?'$':s.currency+' '}${s.price!=null?s.price.toFixed(2):'—'}</span>`},
    {h:'Cambio $',  sk:'chg',        r:(s)=>`<span class="${cls(s.chg)}">${arr(s.chg)}${s.chg!=null?Math.abs(s.chg).toFixed(2):'—'}</span>`},
    {h:'Cambio %',  sk:'chgp',       r:(s)=>`<span class="${cls(s.chgp)}">${arr(s.chgp)}${s.chgp!=null?Math.abs(s.chgp).toFixed(2)+'%':'—'}</span>`},
    {h:'Vol Día',   sk:'volume',     r:(s)=>fVol(s.volume)},
    {h:'RVOL',      sk:'rvol',       r:(s)=>rvolCell(s.rvol)},
    {h:'ATR 14',    sk:'atr14',      r:(s)=>s.atr14!=null?`<span class="muted">$${s.atr14.toFixed(2)}</span>`:'<span class="na">—</span>'},
    {h:'Mkt Cap',   sk:'mktcap_b',   r:(s)=>fCap(s.mktcap_b)},
    {h:'vs 52W↑',   sk:'pct_from_52h', r:(s)=>fPct(s.pct_from_52h)},
    {h:'Max Día',   sk:'high_day',   r:(s)=>f(s.high_day,2,'$')},
    {h:'Min Día',   sk:'low_day',    r:(s)=>f(s.low_day,2,'$')},
    {h:'Beta',      sk:'beta',       r:(s)=>f(s.beta,2)},
    {h:'Sector',    sk:'sector',     r:(s)=>`<span class="stag">${s.sector||'—'}</span>`},
  ],
  valuation: [
    {h:'',       sk:null,         r:(s)=>src(s.source),            left:true, pick:false},
    {h:'Pick',   sk:null,         r:(s)=>'__pick__',               left:true, pick:true},
    {h:'Ticker', sk:'ticker',     r:(s)=>`<span class="ticker" style="cursor:pointer" onclick="openNews('${s.ticker}','${s.name.replace(/'/g,'')}')" title="Ver noticias">${s.ticker} 📰</span>`, left:true},
    {h:'Empresa',sk:'name',       r:(s)=>`<span class="tname" title="${s.name}">${s.name}</span>`, left:true},
    {h:'7 días', sk:null,         r:(s)=>sparklineSVG(s.sparkline),  left:true},
    {h:'Precio', sk:'price',      r:(s)=>`<span class="price">${s.price!=null?'$'+s.price.toFixed(2):'—'}</span>`},
    {h:'Cambio %',sk:'chgp',      r:(s)=>`<span class="${cls(s.chgp)}">${arr(s.chgp)}${s.chgp!=null?Math.abs(s.chgp).toFixed(2)+'%':'—'}</span>`},
    {h:'Mkt Cap', sk:'mktcap_b',  r:(s)=>fCap(s.mktcap_b)},
    {h:'P/E Trail',sk:'pe_trail', r:(s)=>f(s.pe_trail,1,'','x')},
    {h:'P/E Fwd', sk:'pe_fwd',    r:(s)=>f(s.pe_fwd,1,'','x')},
    {h:'P/B',     sk:'pb',        r:(s)=>f(s.pb,1,'','x')},
    {h:'P/S',     sk:'ps',        r:(s)=>f(s.ps,1,'','x')},
    {h:'EPS TTM', sk:'eps',       r:(s)=>f(s.eps,2,'$')},
    {h:'Div Yield',sk:'div_yield',r:(s)=>s.div_yield!=null?`<span class="pos">${s.div_yield.toFixed(2)}%</span>`:'<span class="na">—</span>'},
    {h:'52W Alto', sk:'w52h',     r:(s)=>f(s.w52h,2,'$')},
    {h:'52W Bajo', sk:'w52l',     r:(s)=>f(s.w52l,2,'$')},
    {h:'vs 52W↑', sk:'pct_from_52h', r:(s)=>fPct(s.pct_from_52h)},
  ],
  growth: [
    {h:'',        sk:null,        r:(s)=>src(s.source),            left:true, pick:false},
    {h:'Pick',    sk:null,        r:(s)=>'__pick__',               left:true, pick:true},
    {h:'Ticker',  sk:'ticker',    r:(s)=>`<span class="ticker" style="cursor:pointer" onclick="openNews('${s.ticker}','${s.name.replace(/'/g,'')}')" title="Ver noticias">${s.ticker} 📰</span>`, left:true},
    {h:'Empresa', sk:'name',      r:(s)=>`<span class="tname" title="${s.name}">${s.name}</span>`, left:true},
    {h:'7 días',  sk:null,        r:(s)=>sparklineSVG(s.sparkline),  left:true},
    {h:'Precio',  sk:'price',     r:(s)=>`<span class="price">${s.price!=null?'$'+s.price.toFixed(2):'—'}</span>`},
    {h:'Cambio %',sk:'chgp',      r:(s)=>`<span class="${cls(s.chgp)}">${arr(s.chgp)}${s.chgp!=null?Math.abs(s.chgp).toFixed(2)+'%':'—'}</span>`},
    {h:'Rev Growth YoY',sk:'rev_growth',   r:(s)=>fPct(s.rev_growth!=null?s.rev_growth*100:null)},
    {h:'EPS Growth YoY',sk:'eps_growth',   r:(s)=>fPct(s.eps_growth!=null?s.eps_growth*100:null)},
    {h:'Margen Bruto',  sk:'gross_margin', r:(s)=>s.gross_margin!=null?`<span class="${s.gross_margin>=0?'pos':'neg'}">${(s.gross_margin*100).toFixed(1)}%</span>`:'<span class="na">—</span>'},
    {h:'Margen Neto',   sk:'net_margin',   r:(s)=>s.net_margin!=null?`<span class="${s.net_margin>=0?'pos':'neg'}">${(s.net_margin*100).toFixed(1)}%</span>`:'<span class="na">—</span>'},
    {h:'ROE',      sk:'roe',      r:(s)=>s.roe!=null?`<span class="${s.roe>=0?'pos':'neg'}">${(s.roe*100).toFixed(1)}%</span>`:'<span class="na">—</span>'},
    {h:'ROA',      sk:'roa',      r:(s)=>s.roa!=null?`<span class="${s.roa>=0?'pos':'neg'}">${(s.roa*100).toFixed(1)}%</span>`:'<span class="na">—</span>'},
    {h:'Deuda/Eq', sk:'debt_eq',  r:(s)=>f(s.debt_eq,2,'','x')},
    {h:'Beta',     sk:'beta',     r:(s)=>f(s.beta,2)},
    {h:'Sector',   sk:'sector',   r:(s)=>`<span class="stag">${s.sector||'—'}</span>`},
  ],
  technical: [
    {h:'',          sk:null,          r:(s)=>src(s.source),             left:true, pick:false},
    {h:'Pick',      sk:null,          r:(s)=>'__pick__',                left:true, pick:true},
    {h:'Ticker',    sk:'ticker',      r:(s)=>`<span class="ticker" style="cursor:pointer" onclick="openNews('${s.ticker}','${s.name.replace(/'/g,'')}')" title="Ver noticias">${s.ticker} 📰</span>`, left:true},
    {h:'Precio',    sk:'price',       r:(s)=>`<span class="price">${s.price!=null?'$'+s.price.toFixed(2):'—'}</span>`},
    {h:'Cambio %',  sk:'chgp',        r:(s)=>`<span class="${cls(s.chgp)}">${arr(s.chgp)}${s.chgp!=null?Math.abs(s.chgp).toFixed(2)+'%':'—'}</span>`},
    {h:'Señal Técnica', sk:'tech_signal', r:(s)=>sigCell(s.tech_signal)},
    {h:'RSI 14 Diario',  sk:'rsi_d',  r:(s)=>rsiCell(s.rsi_d)},
    {h:'RSI 14 Semanal', sk:'rsi_w',  r:(s)=>rsiCell(s.rsi_w)},
    {h:'vs EMA 200',     sk:'pct_e200', r:(s)=>emaCell(s.pct_e200)},
    {h:'EMA 200',        sk:'ema200',  r:(s)=>f(s.ema200,2,'$')},
    {h:'EMA 50',         sk:'ema50',   r:(s)=>f(s.ema50,2,'$')},
    {h:'EMA 50/200',     sk:'golden',  r:(s)=>crossCell(s.golden)},
    {h:'MACD',           sk:'macd_bull', r:(s)=>macdCell(s.macd_bull, s.macd_hist)},
    {h:'ATR 14',         sk:'atr14',   r:(s)=>s.atr14!=null?`<span class="muted">$${s.atr14.toFixed(2)}</span>`:'<span class="na">—</span>'},
    {h:'RVOL',           sk:'rvol',    r:(s)=>rvolCell(s.rvol)},
    {h:'Mkt Cap',        sk:'mktcap_b', r:(s)=>fCap(s.mktcap_b)},
    {h:'Beta',           sk:'beta',    r:(s)=>f(s.beta,2)},
    {h:'vs 52W↑',        sk:'pct_from_52h', r:(s)=>fPct(s.pct_from_52h)},
  ],
  analysts: [
    {h:'',        sk:null,        r:(s)=>src(s.source),            left:true, pick:false},
    {h:'Pick',    sk:null,        r:(s)=>'__pick__',               left:true, pick:true},
    {h:'Ticker',  sk:'ticker',    r:(s)=>`<span class="ticker" style="cursor:pointer" onclick="openNews('${s.ticker}','${s.name.replace(/'/g,'')}')" title="Ver noticias">${s.ticker} 📰</span>`, left:true},
    {h:'Empresa', sk:'name',      r:(s)=>`<span class="tname" title="${s.name}">${s.name}</span>`, left:true},
    {h:'Precio',  sk:'price',     r:(s)=>`<span class="price">${s.price!=null?'$'+s.price.toFixed(2):'—'}</span>`},
    {h:'Cambio %',sk:'chgp',      r:(s)=>`<span class="${cls(s.chgp)}">${arr(s.chgp)}${s.chgp!=null?Math.abs(s.chgp).toFixed(2)+'%':'—'}</span>`},
    {h:'Score Analistas', sk:'analyst_score', r:(s)=>analystScore(s.analyst_score)},
    {h:'Score Composite',sk:'_score', r:(s)=>s._score!=null?`<span class="score-pill">${s._score.toFixed(1)}</span>`:'<span class="na">—</span>'},
    {h:'Buy/Hold/Sell', sk:'analyst_buy', r:(s)=>analystBar(s.analyst_buy,s.analyst_hold,s.analyst_sell)},
    {h:'Precio Obj.', sk:'price_target', r:(s)=>f(s.price_target,2,'$')},
    {h:'Upside',  sk:'upside',    r:(s)=>{
      if(s.upside==null)return'<span class="na">—</span>';
      return`<span class="${s.upside>=0?'pos-bg':'neg-bg'}">${s.upside>=0?'+':''}${s.upside.toFixed(1)}%</span>`;
    }},
    {h:'Mkt Cap', sk:'mktcap_b',  r:(s)=>fCap(s.mktcap_b)},
    {h:'P/E Fwd', sk:'pe_fwd',    r:(s)=>f(s.pe_fwd,1,'','x')},
    {h:'vs 52W↑', sk:'pct_from_52h', r:(s)=>fPct(s.pct_from_52h)},
    {h:'Sector',  sk:'sector',    r:(s)=>`<span class="stag">${s.sector||'—'}</span>`},
  ],
};

/* ── state ── */
function setGroup(g,btn){
  activeGroup=g;
  document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active'); renderAll();
}
function setTab(t,el){
  activeTab=t; sortCol=null;
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  if(t==='market') renderMarket();
  else if(t==='mining') renderMining();
  else if(t==='options') renderOptions();
  else renderAll();
}
function sortBy(k){
  if(sortCol===k)sortDir*=-1; else{sortCol=k;sortDir=1;}
  renderAll();
}

/* ── filter + sort ── */
function visible(){
  const q=document.getElementById('search').value.toLowerCase();
  const thr=parseFloat(document.getElementById('thr').value)||3;
  let rows=allData.filter(s=>{
    if(activeGroup!=='all'&&s.group!==activeGroup)return false;
    if(q&&!s.ticker.toLowerCase().includes(q)&&!s.name.toLowerCase().includes(q))return false;
    return true;
  });
  if(sortCol){
    rows=[...rows].sort((a,b)=>{
      let va=a[sortCol],vb=b[sortCol];
      if(va==null)return 1; if(vb==null)return -1;
      return typeof va==='string'?va.localeCompare(vb)*sortDir:(va-vb)*sortDir;
    });
  }
  return{rows,thr};
}

/* ── render ── */
function buildSection(title,rows,cols,thr){
  if(!rows.length)return'';
  const ths=cols.map(c=>{
    if(!c.sk)return`<th class="no-sort left"></th>`;
    const sc=sortCol===c.sk?(sortDir===1?'sort-asc':'sort-desc'):'';
    const lft=c.left?' left':'';
    return`<th class="${sc}${lft}" onclick="sortBy('${c.sk}')">${c.h}</th>`;
  }).join('');
  const trs=rows.map(s=>{
    const gc=s.group==='requested'?'req':s.group==='recommended'?'rec':'ai';
    const chgAbs=Math.abs(s.chgp||0);
    const alertCls=chgAbs>=thr?(s.chgp>=0?' alert-hi':' alert-lo'):'';
    const chgd=prevPrices[s.ticker]!=null&&prevPrices[s.ticker]!==s.price;
    const flashCls=chgd?(s.chg>=0?' flash-up':' flash-dn'):'';
    prevPrices[s.ticker]=s.price;
    const pickCls = s._pick?' pick-row':'';
    const tds=cols.map(c=>{
      if(c.pick) return pickCell(s);
      return`<td class="${c.left?'left':''}">${c.r(s)}</td>`;
    }).join('');
    return`<tr class="${gc}${alertCls}${flashCls}${pickCls}">${tds}</tr>`;
  }).join('');
  return`<div class="section-title">${title} <span style="color:#1e3a50">(${rows.length})</span></div>
<div class="table-wrap"><table>
<thead><tr>${ths}</tr></thead>
<tbody>${trs}</tbody>
</table></div>`;
}

function renderAll(){
  const{rows,thr}=visible();
  const cols=TABS[activeTab];
  const req=rows.filter(s=>s.group==='requested');
  const rec=rows.filter(s=>s.group==='recommended');
  const ai =rows.filter(s=>s.group==='ai');
  let html='';
  if(activeGroup==='all'){
    html=buildSection('📌 SOLICITADAS',req,cols,thr)
       +buildSection('💡 RECOMENDADAS',rec,cols,thr)
       +buildSection('🚀 IA / CRECIMIENTO',ai,cols,thr);
  } else {
    const titles={requested:'📌 SOLICITADAS',recommended:'💡 RECOMENDADAS',ai:'🚀 IA / CRECIMIENTO'};
    html=buildSection(titles[activeGroup]||'',rows,cols,thr);
  }
  if(!rows.length)html='<div style="text-align:center;padding:40px;color:#304050">Sin resultados.</div>';
  document.getElementById('root').innerHTML=html;
}

/* ── sparkline ── */
function sparklineSVG(prices, w=90, h=30){
  if(!prices||prices.length<3) return '<span class="na" style="font-size:.65rem">sin datos</span>';
  const min=Math.min(...prices), max=Math.max(...prices);
  const range=max-min||1;
  const pad=3;
  const pts=prices.map((p,i)=>{
    const x=(i/(prices.length-1))*(w);
    const y=h-pad-((p-min)/range)*(h-pad*2);
    return`${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const up=prices[prices.length-1]>=prices[0];
  const col=up?'#2ecc71':'#e74c3c';
  const fill=up?'rgba(46,204,113,0.12)':'rgba(231,76,60,0.12)';
  // fill polygon down to baseline
  const first=`0,${h}`, last=`${w},${h}`;
  const pct=((prices[prices.length-1]-prices[0])/prices[0]*100);
  const label=(pct>=0?'+':'')+pct.toFixed(1)+'%';
  return`<span class="spark" title="7 días: ${label}">
    <svg width="${w}" height="${h}" style="display:block;overflow:visible">
      <polygon points="${first} ${pts} ${last}" fill="${fill}" stroke="none"/>
      <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6"
        stroke-linejoin="round" stroke-linecap="round"/>
    </svg>
  </span>`;
}

/* ── news panel ── */
let newsOpen=false;
async function openNews(ticker, name){
  document.getElementById('news-ticker-label').textContent=ticker;
  document.getElementById('news-name-label').textContent=name+' · últimos 7 días';
  document.getElementById('news-list').innerHTML='<div class="news-loading">⏳ Cargando noticias...</div>';
  document.getElementById('news-panel').classList.add('open');
  document.getElementById('news-overlay').classList.add('open');
  newsOpen=true;
  try{
    const j=await(await fetch(`/api/news/${ticker}`)).json();
    const items=j.news||[];
    if(!items.length){
      document.getElementById('news-list').innerHTML='<div class="news-loading">Sin noticias recientes.</div>';
      return;
    }
    document.getElementById('news-list').innerHTML=items.map(n=>`
      <div class="news-item">
        ${n.image?`<img class="news-img" src="${n.image}" onerror="this.style.display='none'">` : ''}
        <a class="news-headline" href="${n.url}" target="_blank" rel="noopener">${n.headline}</a>
        <div class="news-meta">
          <span class="news-source">${n.source}</span>
          <span class="news-ago">${n.ago}</span>
        </div>
        ${n.summary?`<div class="news-summary">${n.summary}${n.summary.length>=220?'…':''}</div>`:''}
      </div>`).join('');
  }catch(e){
    document.getElementById('news-list').innerHTML=`<div class="news-loading">Error: ${e.message}</div>`;
  }
}
function closeNews(){
  document.getElementById('news-panel').classList.remove('open');
  document.getElementById('news-overlay').classList.remove('open');
  newsOpen=false;
}

/* ── Market Summary helpers ── */
let mktData = null;

function biasChip(b){
  if(b==='long')    return '<span class="bias-long">▲ LONG</span>';
  if(b==='short')   return '<span class="bias-short">▼ SHORT</span>';
  return '<span class="bias-neutral">— NEUTRAL</span>';
}

function fInst(v, sym){
  // For yields and DXY omit $ sign
  const noSign = ['vix','us10y','us30y','dxy'].includes(sym);
  if(v==null) return '<span class="na">—</span>';
  return noSign ? v.toFixed(2) : '$'+v.toFixed(2);
}

function pivotHtml(pivots, label){
  if(!pivots) return '<span class="na" style="font-size:.72rem">Sin datos</span>';
  return `<div style="margin-top:4px">
    <div style="font-size:.65rem;color:#304050;margin-bottom:4px">${label}</div>
    <div class="pivot-grid">
      <div class="pivot-item res"><div class="plabel">R3</div><div class="pval">${pivots.R3.toFixed(1)}</div></div>
      <div class="pivot-item res"><div class="plabel">R2</div><div class="pval">${pivots.R2.toFixed(1)}</div></div>
      <div class="pivot-item res"><div class="plabel">R1</div><div class="pval">${pivots.R1.toFixed(1)}</div></div>
      <div class="pivot-item pvt"><div class="plabel">Pivot</div><div class="pval">${pivots.pivot.toFixed(1)}</div></div>
      <div class="pivot-item sup"><div class="plabel">S1</div><div class="pval">${pivots.S1.toFixed(1)}</div></div>
      <div class="pivot-item sup"><div class="plabel">S2</div><div class="pval">${pivots.S2.toFixed(1)}</div></div>
    </div>
  </div>`;
}

function formatMktTime(ts){
  if(!ts) return '';
  // accepts Unix timestamp (number) or ISO string
  const d    = typeof ts==='number' ? new Date(ts*1000) : new Date(ts);
  const diff = (Date.now() - d.getTime()) / 1000;
  if(isNaN(diff)) return ts;
  if(diff < 60)    return 'ahora';
  if(diff < 3600)  return `hace ${Math.round(diff/60)}m`;
  if(diff < 86400) return `hace ${Math.round(diff/3600)}h`;
  return d.toLocaleDateString('es-AR',{day:'2-digit',month:'2-digit'});
}

async function renderMarket(){
  document.getElementById('root').innerHTML='<div class="mkt-loading">⏳ Cargando resumen de mercado...</div>';
  try{
    const j = await(await fetch('/api/market')).json();
    mktData = j;
  } catch(e){
    document.getElementById('root').innerHTML=`<div class="mkt-loading">Error cargando datos: ${e.message}</div>`;
    return;
  }
  if(!mktData || Object.keys(mktData).length===0){
    document.getElementById('root').innerHTML='<div class="mkt-loading">⏳ Los datos de mercado se están calculando por primera vez (~60s)...</div>';
    setTimeout(renderMarket, 8000);
    return;
  }

  const INST_META = {
    sp500:{label:'S&P 500', name:'SPDR S&P 500'},
    qqq:  {label:'QQQ',     name:'Invesco QQQ (Nasdaq 100)'},
    vix:  {label:'VIX',     name:'CBOE Volatility Index'},
    us10y:{label:'US 10Y',  name:'Tasa 10 años Treasury'},
    us30y:{label:'US 30Y',  name:'Tasa 30 años Treasury'},
    dxy:  {label:'DXY',     name:'US Dollar Index Futures'},
    gold: {label:'Gold',    name:'Oro ($/oz)'},
    oil:  {label:'WTI',     name:'Petróleo Crudo WTI'},
  };

  // ── 1. Macro ──────────────────────────────────────────────────────────────
  let macroRows='';
  for(const [key,meta] of Object.entries(INST_META)){
    const d=mktData[key]; if(!d) continue;
    const noSign=['vix','us10y','us30y','dxy'].includes(key);
    const pStr = noSign ? d.price.toFixed(2) : '$'+d.price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    const suf  = (key==='us10y'||key==='us30y') ? '%' : '';
    const cc   = d.chgp>=0?'pos':'neg';
    macroRows+=`<div class="mkt-inst-row">
      <span class="mkt-inst-sym">${meta.label}</span>
      <span class="mkt-inst-name">${meta.name}</span>
      <span class="mkt-inst-price">${pStr}${suf}</span>
      <span class="mkt-inst-chg ${cc}">${d.chgp>=0?'+':''}${d.chgp.toFixed(2)}%</span>
    </div>`;
  }

  // ── 2. Fear & Greed ───────────────────────────────────────────────────────
  const FG_LABELS = {
    'extreme greed':'CODICIA EXTREMA','greed':'CODICIA',
    'neutral':'NEUTRAL','fear':'MIEDO','extreme fear':'MIEDO EXTREMO'
  };
  let fgHtml='<span class="na">Sin datos</span>';
  if(mktData.fear_greed && mktData.fear_greed.score!=null){
    const fg=mktData.fear_greed;
    const sc=fg.score;
    let cls2='fg-neutral', barCol='#f0c040';
    const rawLbl=(fg.rating||'').toLowerCase();
    const lbl = FG_LABELS[rawLbl] || fg.rating.toUpperCase() || '';
    if(sc>=75){cls2='fg-greed'; barCol='#2ecc71';}
    else if(sc>=55){cls2='fg-fgreed'; barCol='#90d050';}
    else if(sc>=45){cls2='fg-neutral'; barCol='#f0c040';}
    else if(sc>=25){cls2='fg-fear';  barCol='#e09040';}
    else{cls2='fg-xfear'; barCol='#e74c3c';}
    const prevStr = fg.prev!=null ? `<div style="font-size:.68rem;color:#405060;margin-top:4px">Cierre ant: ${fg.prev.toFixed(0)}</div>` : '';
    const srcStr  = fg.synthetic ? `<div style="font-size:.60rem;color:#2a4050;margin-top:2px">calculado: VIX · P/C · Breadth · TRIN</div>` : '';
    fgHtml=`<div class="fg-meter">
      <div class="fg-score ${cls2}">${sc.toFixed(0)}</div>
      <div class="fg-label ${cls2}">${lbl}</div>
      <div class="fg-bar-wrap"><div class="fg-bar" style="width:${sc}%;background:${barCol}"></div></div>
      ${prevStr}${srcStr}
      <div style="display:flex;justify-content:space-between;width:100%;font-size:.62rem;color:#304050;margin-top:3px">
        <span>0 Miedo Extremo</span><span>100 Codicia Extrema</span>
      </div>
    </div>`;
  }

  // ── 3. Bias + Pivots ──────────────────────────────────────────────────────
  const sp=mktData.sp500||{}, qq=mktData.qqq||{};
  const biasHtml=`
    <div class="bias-row"><span class="bias-sym">SP500</span>
      <span class="bias-label">4H</span>${biasChip(sp.h4_bias)}
      <span class="bias-label" style="margin-left:8px">Diario</span>${biasChip(sp.daily_bias)}
    </div>
    <div class="bias-row"><span class="bias-sym">QQQ</span>
      <span class="bias-label">4H</span>${biasChip(qq.h4_bias)}
      <span class="bias-label" style="margin-left:8px">Diario</span>${biasChip(qq.daily_bias)}
    </div>
    <div style="font-size:.62rem;color:#203040;margin-top:6px">EMA20/50 · RSI · MACD — no constituye consejo de inversión.</div>`;

  const pivotsHtml=`
    ${pivotHtml(sp.pivots,'S&P 500 — Floor Pivots')}
    <div style="margin-top:8px"></div>
    ${pivotHtml(qq.pivots,'QQQ — Floor Pivots')}`;

  // ── 4. Sector Heatmap ─────────────────────────────────────────────────────
  const sectors=mktData.sectors||[];
  let sectorHtml='<div class="sector-grid">';
  sectors.forEach(s=>{
    const pct=s.chgp; const pos=pct>=0;
    const intensity=Math.min(Math.abs(pct)/3,1);
    const bg=pos
      ? `rgba(46,204,113,${0.08+intensity*0.25})`
      : `rgba(231,76,60,${0.08+intensity*0.25})`;
    const col=pos?`hsl(145,${40+intensity*50}%,${50+intensity*10}%)`:`hsl(0,${40+intensity*50}%,${55+intensity*10}%)`;
    sectorHtml+=`<div class="sector-cell" style="background:${bg};border:1px solid ${pos?'rgba(46,204,113,.2)':'rgba(231,76,60,.2)'}">
      <div class="sector-sym">${s.sym}</div>
      <div class="sector-name">${s.name}</div>
      <div class="sector-chg" style="color:${col}">${pct>=0?'+':''}${pct.toFixed(2)}%</div>
    </div>`;
  });
  sectorHtml+='</div>';
  if(!sectors.length) sectorHtml='<span class="na">Cargando sectores...</span>';

  // ── 5. Market Breadth ─────────────────────────────────────────────────────
  const br=mktData.breadth||{};
  const adRatio = (br.adv&&br.dec) ? (br.adv/(br.adv+br.dec)*100).toFixed(0) : null;
  const pcRatio  = br.pcall;
  const pcCls = pcRatio ? (pcRatio>1.1?'pos':(pcRatio<0.7?'neg':'neu')) : '';
  let breadthHtml=`<div class="breadth-grid">
    <div class="breadth-item"><div class="breadth-label">NYSE Avances</div>
      <div class="breadth-val pos">${br.adv!=null?br.adv.toLocaleString():'—'}</div></div>
    <div class="breadth-item"><div class="breadth-label">NYSE Bajas</div>
      <div class="breadth-val neg">${br.dec!=null?br.dec.toLocaleString():'—'}</div></div>
    <div class="breadth-item"><div class="breadth-label">Nuevos Máx 52W</div>
      <div class="breadth-val pos">${br.nhigh!=null?br.nhigh.toLocaleString():'—'}</div></div>
    <div class="breadth-item"><div class="breadth-label">Nuevos Mín 52W</div>
      <div class="breadth-val neg">${br.nlow!=null?br.nlow.toLocaleString():'—'}</div></div>
  </div>
  ${adRatio!=null?`<div style="margin-top:8px;font-size:.72rem;color:#607080">
    Relación Avance/Baja: <strong style="color:${adRatio>=50?'#2ecc71':'#e74c3c'}">${adRatio}% alcistas</strong>
  </div>`:''}
  ${pcRatio!=null?`<div style="margin-top:6px;font-size:.72rem;color:#607080">
    Put/Call Ratio: <strong class="${pcCls}">${pcRatio.toFixed(2)}</strong>
    <span style="color:#304050"> (>1.1 miedo · <0.7 euforia)</span>
  </div>`:''}
  ${br.trin!=null?`<div style="margin-top:4px;font-size:.72rem;color:#607080">
    TRIN (Arms Index): <strong>${br.trin.toFixed(2)}</strong>
    <span style="color:#304050"> (>1.5 presión vendedora · <0.5 compradora)</span>
  </div>`:''}`;

  // ── 6. Economic Calendar (mktnews) ───────────────────────────────────────
  const cal=mktData.econ_calendar||[];
  let calHtml='';
  if(cal.length){
    calHtml=`<div style="display:grid;grid-template-columns:30px 110px 1fr 45px 75px 75px 75px;gap:4px;
      font-size:.65rem;color:#304050;padding-bottom:5px;border-bottom:1px solid #0e1e2e;margin-bottom:4px">
      <span></span><span>Hora</span><span>Evento</span><span>★</span>
      <span style="text-align:right">Actual</span>
      <span style="text-align:right">Estim.</span>
      <span style="text-align:right">Previo</span>
    </div>`;
    cal.forEach(e=>{
      const impIcon = e.impact==='high'?'🔴':e.impact==='medium'?'🟡':'⚪';
      const stars   = '★'.repeat(e.star||1)+'☆'.repeat(3-(e.star||1));
      const timeStr = (e.time||'').replace('T',' ').substring(0,16)||'—';
      const unit    = e.unit||'';
      const fmtVal  = v => v!=null ? `${v}${unit}` : '<span class="na">—</span>';
      const actCls  = e.affect==='POSITIVE'?'cal-act pos':(e.affect==='NEGATIVE'?'neg':'');
      const actHtml = e.actual!=null
        ? `<span class="${actCls}">${e.actual}${unit}</span>`
        : '<span class="na">—</span>';
      calHtml+=`<div class="cal-row" style="grid-template-columns:30px 110px 1fr 45px 75px 75px 75px">
        <span title="${e.country}">${e.flag||'🌐'}</span>
        <span class="cal-time">${timeStr}</span>
        <span class="cal-event">${e.event}</span>
        <span class="${e.impact==='high'?'cal-imp-high':e.impact==='medium'?'cal-imp-medium':'cal-imp-low'}" title="${e.impact}">${impIcon}</span>
        <span class="cal-val">${actHtml}</span>
        <span class="cal-val" style="color:#506070">${fmtVal(e.estimate)}</span>
        <span class="cal-val" style="color:#405060">${fmtVal(e.prev)}</span>
      </div>`;
    });
  } else {
    calHtml='<span class="na" style="font-size:.76rem">Cargando eventos económicos...</span>';
  }

  // ── 7. Earnings Calendar ──────────────────────────────────────────────────
  const earnings=mktData.earnings||[];
  let earnHtml='';
  if(earnings.length){
    earnHtml='<div style="display:grid;grid-template-columns:55px 75px 45px 85px 85px;gap:4px;font-size:.66rem;color:#304050;padding-bottom:4px;border-bottom:1px solid #0e1e2e;margin-bottom:4px"><span>Ticker</span><span>Fecha</span><span>Hora</span><span style="text-align:right">EPS Est.</span><span style="text-align:right">Rev Est.</span></div>';
    earnings.forEach(e=>{
      const hourCls=e.hour==='bmo'?'earn-bmo':e.hour==='amc'?'earn-amc':'earn-dmh';
      const hourLbl=e.hour==='bmo'?'Pre-Mkt':e.hour==='amc'?'Post-Mkt':'Durante';
      const epsStr=e.eps_est!=null?`$${Number(e.eps_est).toFixed(2)}`:'—';
      const revStr=e.rev_est!=null?`$${(Number(e.rev_est)/1e9).toFixed(1)}B`:'—';
      earnHtml+=`<div class="earn-row">
        <span class="earn-ticker">${e.ticker}</span>
        <span class="earn-date">${e.date}</span>
        <span class="earn-hour ${hourCls}">${hourLbl}</span>
        <span style="text-align:right">${epsStr}</span>
        <span style="text-align:right;color:#607080">${revStr}</span>
      </div>`;
    });
  } else {
    earnHtml='<span class="na" style="font-size:.76rem">Sin earnings próximos en tickers seguidos.</span>';
  }

  // ── 8. Pre-market movers ──────────────────────────────────────────────────
  const preMov=mktData.pre_movers||[];
  let preHtml='';
  if(preMov.length){
    const preUp=preMov.filter(m=>m.chgp>0).slice(0,8);
    const preDn=preMov.filter(m=>m.chgp<0).slice(0,8);
    preHtml=`<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div>
        <div style="font-size:.66rem;color:#304050;margin-bottom:4px">▲ SUBIENDO</div>
        ${preUp.map(m=>`<div class="pre-row">
          <span class="pre-ticker">${m.ticker}</span>
          <span style="color:#607080;font-size:.74rem">$${m.price.toFixed(2)}</span>
          <span class="pos">+${m.chgp.toFixed(2)}%</span>
        </div>`).join('')}
      </div>
      <div>
        <div style="font-size:.66rem;color:#304050;margin-bottom:4px">▼ BAJANDO</div>
        ${preDn.map(m=>`<div class="pre-row">
          <span class="pre-ticker">${m.ticker}</span>
          <span style="color:#607080;font-size:.74rem">$${m.price.toFixed(2)}</span>
          <span class="neg">${m.chgp.toFixed(2)}%</span>
        </div>`).join('')}
      </div>
    </div>`;
  } else {
    preHtml='<span class="na" style="font-size:.76rem">Sin movers pre-market significativos (&gt;1%).</span>';
  }

  // ── 9. Top movers ─────────────────────────────────────────────────────────
  const gainers=(mktData.top_gainers||[]).slice(0,15);
  const losers =(mktData.top_losers ||[]).slice(0,15);
  const moverTable=(arr,pos)=>{
    let h='<table class="movers-table">';
    arr.forEach(m=>{
      h+=`<tr><td class="movers-ticker">${m.ticker}</td>
        <td style="text-align:right;color:#507080">$${m.price.toFixed(2)}</td>
        <td style="text-align:right" class="${pos?'pos':'neg'}">${pos?'▲':'▼'} ${Math.abs(m.chgp).toFixed(2)}%</td></tr>`;
    });
    return h+'</table>';
  };

  // ── 10. News (mktnews) ───────────────────────────────────────────────────
  const news=mktData.news||[];
  const impactColor={'bullish':'#2ecc71','bearish':'#e74c3c','mixed':'#f0c040'};
  let newsHtml=news.map(n=>{
    const hotBadge = n.hot
      ? '<span style="background:#2a1a00;color:#f0a030;border:1px solid #5a3a00;border-radius:3px;padding:0 5px;font-size:.64rem;font-weight:700;margin-right:5px">🔥 HOT</span>'
      : '';
    const impBadge = n.important
      ? '<span style="background:#2a0a2a;color:#c060e0;border:1px solid #5a1a5a;border-radius:3px;padding:0 5px;font-size:.64rem;font-weight:700;margin-right:5px">⚡ IMPORTANTE</span>'
      : '';
    const impactsHtml = (n.impacts||[]).map(i=>{
      const col = impactColor[i.impact]||'#607080';
      return `<span style="color:${col};font-size:.64rem;margin-right:6px">${i.impact==='bullish'?'▲':'▼'} ${i.symbol}</span>`;
    }).join('');
    const timeStr = n.datetime ? formatMktTime(n.datetime) : '';
    const srcStr  = n.source ? `<span style="color:#2a6aaa;font-size:.67rem;font-weight:600;background:#0a1e36;padding:1px 5px;border-radius:3px">${n.source}</span>` : '';
    const linkOpen  = n.url&&n.url!='#' ? `<a class="news-mkt-hl" href="${n.url}" target="_blank" rel="noopener">` : '<span class="news-mkt-hl" style="cursor:default">';
    const linkClose = n.url&&n.url!='#' ? '</a>' : '</span>';
    return `<div class="news-mkt-item">
      ${hotBadge}${impBadge}
      ${linkOpen}${n.headline}${linkClose}
      <div class="news-mkt-meta" style="margin-top:3px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        ${srcStr}
        <span style="color:#304050">${timeStr}</span>
        ${impactsHtml}
      </div>
    </div>`;
  }).join('') || '<span class="na">Cargando noticias...</span>';

  // ── Render ────────────────────────────────────────────────────────────────
  document.getElementById('root').innerHTML=`
  <div style="padding:6px 18px 0;font-size:.68rem;color:#1e3040;text-align:right">
    Actualizado: ${mktData.updated_at||'—'} &nbsp;·&nbsp;
    <span style="cursor:pointer;color:#2a6aaa" onclick="renderMarket()">↻ Refrescar</span>
  </div>
  <div class="mkt-grid">

    <div class="mkt-card" style="grid-column:span 2">
      <div class="mkt-card-title"><span>📊</span> ÍNDICES · TASAS · MACRO</div>
      ${macroRows}
    </div>

    <div class="mkt-card">
      <div class="mkt-card-title"><span>😱</span> FEAR &amp; GREED INDEX</div>
      ${fgHtml}
    </div>

    <div class="mkt-card">
      <div class="mkt-card-title"><span>🧭</span> BIAS TÉCNICO</div>
      ${biasHtml}
    </div>

    <div class="mkt-card" style="grid-column:span 2">
      <div class="mkt-card-title"><span>🌡️</span> HEATMAP DE SECTORES S&amp;P500</div>
      ${sectorHtml}
    </div>

    <div class="mkt-card">
      <div class="mkt-card-title"><span>📐</span> SOPORTES &amp; RESISTENCIAS</div>
      ${pivotsHtml}
      <div style="font-size:.62rem;color:#203040;margin-top:6px">Pivots clásicos Floor — OHLC sesión previa.</div>
    </div>

    <div class="mkt-card">
      <div class="mkt-card-title"><span>📡</span> AMPLITUD DE MERCADO</div>
      ${breadthHtml}
    </div>

    <div class="mkt-card" style="grid-column:span 2">
      <div class="mkt-card-title"><span>🌅</span> PRE-MARKET MOVERS</div>
      ${preHtml}
    </div>

    <div class="mkt-card">
      <div class="mkt-card-title"><span>🟢</span> TOP GAINERS S&amp;P500 (sesión previa)</div>
      ${moverTable(gainers,true)}
    </div>

    <div class="mkt-card">
      <div class="mkt-card-title"><span>🔴</span> TOP LOSERS S&amp;P500 (sesión previa)</div>
      ${moverTable(losers,false)}
    </div>

    <div class="mkt-card" style="grid-column:span 2">
      <div class="mkt-card-title"><span>📅</span> CALENDARIO ECONÓMICO — PRÓXIMOS 3 DÍAS (EE.UU.)</div>
      ${calHtml}
    </div>

    <div class="mkt-card" style="grid-column:span 2">
      <div class="mkt-card-title"><span>📢</span> EARNINGS — PRÓXIMAS 2 SEMANAS (TICKERS SEGUIDOS)</div>
      ${earnHtml}
    </div>

    <div class="mkt-card" style="grid-column:span 2">
      <div class="mkt-card-title"><span>📰</span> NOTICIAS DE MERCADO</div>
      ${newsHtml}
    </div>

  </div>`;
}

/* ── Materias Primas ── */
const MINING_BADGE = {
  'gold':          ['ORO',     'gold'],
  'silver':        ['PLATA',   'silver'],
  'copper':        ['COBRE',   'copper'],
  'uranium':       ['URANIO',  'uranium'],
  'gold-miner':    ['MINERA ORO',   'miner'],
  'silver-miner':  ['MINERA PLATA', 'miner'],
  'copper-miner':  ['MINERA COBRE', 'miner'],
  'uranium-miner': ['MINERA U',     'miner'],
  'diversified':   ['DIVERSIF.',    'div'],
};
const MINING_GROUPS = [
  {label:'⚡ Materias Primas — Futuros & ETFs', types:['gold','silver','copper','uranium']},
  {label:'🥇 Mineras de Oro',                  types:['gold-miner']},
  {label:'🪨 Mineras de Plata',                types:['silver-miner']},
  {label:'🔩 Mineras de Cobre & Uranio',       types:['copper-miner','uranium-miner']},
  {label:'🌏 Mineras Diversificadas',          types:['diversified']},
];

function mRsi(v){
  if(v==null) return '<span class="na">—</span>';
  const cls = v>=70?'rsi-ob':v<=30?'rsi-os':'rsi-mid';
  return `<span class="${cls}">${v.toFixed(0)}</span>`;
}
function mChg(v){
  if(v==null) return '<span class="na">—</span>';
  const cls = v>=0?'m-chg-pos':'m-chg-neg';
  return `<span class="${cls}">${v>=0?'+':''}${v.toFixed(2)}%</span>`;
}
function mDist(v, invert=false){
  if(v==null) return '<span class="na">—</span>';
  // d30h is always ≤0 (below high); d30l is always ≥0 (above low)
  const cls = (invert ? v<=0 : v>=0) ? 'd-pos' : 'd-neg';
  return `<span class="${cls}">${v>=0?'+':''}${v.toFixed(1)}%</span>`;
}
function mVol(v){
  if(!v) return '<span class="na">—</span>';
  if(v>=1e9) return `<span class="vol-fmt">${(v/1e9).toFixed(1)}B</span>`;
  if(v>=1e6) return `<span class="vol-fmt">${(v/1e6).toFixed(1)}M</span>`;
  if(v>=1e3) return `<span class="vol-fmt">${(v/1e3).toFixed(0)}K</span>`;
  return `<span class="vol-fmt">${v}</span>`;
}
function mPrice(v){
  if(v==null) return '<span class="na">—</span>';
  // futures can be large (gold ~2300) or tiny (copper ~4.5)
  const dec = v>=100?2:v>=1?3:5;
  return `<span class="m-price">${v.toFixed(dec)}</span>`;
}

async function renderMining(){
  document.getElementById('root').innerHTML='<div class="mkt-loading">⏳ Cargando materias primas y mineras...</div>';
  let d;
  try{
    const j=await(await fetch('/api/mining')).json();
    d=j.items||[];
  }catch(e){
    document.getElementById('root').innerHTML=`<div class="mkt-loading">⚠ Error: ${e.message}</div>`;
    return;
  }
  if(!d.length){
    document.getElementById('root').innerHTML='<div class="mkt-loading">⏳ Los datos se están calculando por primera vez (~90s)...</div>';
    setTimeout(renderMining,10000); return;
  }

  const byType={};
  d.forEach(r=>{ (byType[r.type]||(byType[r.type]=[])).push(r); });

  const tableHdr=`<table class="mining-table">
    <thead><tr>
      <th>Activo</th>
      <th class="r">Precio</th>
      <th class="r">Δ Día</th>
      <th class="r">Dist. Máx 30d</th>
      <th class="r">Dist. Mín 30d</th>
      <th style="min-width:80px">Rango 30d</th>
      <th class="r">Dist. Máx 52s</th>
      <th class="r">RSI-14</th>
      <th class="r">ATR% 14</th>
      <th class="r">Volumen</th>
    </tr></thead><tbody>`;

  let html='<div class="mining-wrap">';
  MINING_GROUPS.forEach(grp=>{
    const rows=grp.types.flatMap(t=>byType[t]||[]);
    if(!rows.length) return;
    html+=`<div class="mining-section">
      <div class="mining-section-title">${grp.label}</div>
      ${tableHdr}`;
    rows.forEach(r=>{
      const [blbl,bcls]=MINING_BADGE[r.type]||['—','miner'];
      const pos30pct=r.pos30??50;
      html+=`<tr>
        <td>
          <span class="m-ticker">${r.ticker}</span>
          <span class="m-badge m-badge-${bcls}">${blbl}</span><br>
          <span class="m-name">${r.name}</span>
        </td>
        <td class="r">${mPrice(r.price)}</td>
        <td class="r">${mChg(r.chgp)}</td>
        <td class="r">${mDist(r.d30h,false)}</td>
        <td class="r">${mDist(r.d30l,true)}</td>
        <td>
          <div class="range30-bar" title="Posición en rango 30d: ${pos30pct.toFixed(0)}%">
            <div class="range30-fill" style="width:${pos30pct}%"></div>
          </div>
          <span style="font-size:.65rem;color:#405060;margin-left:4px">${pos30pct.toFixed(0)}%</span>
        </td>
        <td class="r">${mDist(r.d52h,false)}</td>
        <td class="r">${mRsi(r.rsi14)}</td>
        <td class="r">${r.atr_pct!=null?`<span style="color:#607080">${r.atr_pct.toFixed(2)}%</span>`:'<span class="na">—</span>'}</td>
        <td class="r">${mVol(r.volume)}</td>
      </tr>`;
    });
    html+='</tbody></table></div>';
  });
  html+=`<div style="font-size:.62rem;color:#1e3040;padding:4px 2px">
    ATR% = ATR-14 como % del precio — mide volatilidad diaria esperada.
    Rango 30d: posición del precio entre mínimo (0%) y máximo (100%) del mes.
    Actualizado: ${(await(await fetch('/api/mining')).json()).updated_at||'—'}
    &nbsp;·&nbsp;<span style="cursor:pointer;color:#2a6aaa" onclick="renderMining()">↻ Refrescar</span>
  </div>`;
  html+='</div>';
  document.getElementById('root').innerHTML=html;
}

/* ── Opciones ── */
function pcChip(v){
  if(v==null) return '';
  const cls = v<0.7?'opt-pc-bull':v>1.1?'opt-pc-bear':'opt-pc-neu';
  const lbl = v<0.7?'↑ Alcista':v>1.1?'↓ Bajista':'↔ Neutro';
  return `<span class="opt-pc-chip ${cls}">P/C ${v.toFixed(2)} ${lbl}</span>`;
}
function mpDistStr(d){
  if(d==null) return '<span class="na">—</span>';
  const cls = d>2?'mp-dist-pos':d<-2?'mp-dist-neg':'mp-dist-neu';
  return `<span class="${cls}">${d>=0?'+':''}${d.toFixed(1)}%</span>`;
}
function fmtOI(v){
  if(!v && v!==0) return '<span class="na">—</span>';
  if(v>=1e6) return (v/1e6).toFixed(1)+'M';
  if(v>=1e3) return (v/1e3).toFixed(0)+'K';
  return v.toString();
}

/* Flow score 0-1: last vs bid/ask midpoint.
   >0.65 → last near ask → COMPRADO (buyer initiated)
   <0.35 → last near bid → VENDIDO (seller initiated)
   null  → sin datos de bid/ask (ej. Deribit) */
function flowBadge(f, side){
  // side: 'c' for call, 'p' for put
  const isCall = side==='c';
  if(f==null) return '<span style="color:#2a4050;font-size:.62rem">—</span>';
  if(f >= 0.65){
    const meaning = isCall ? '🟢 CALL COMPRADA' : '🟢 PUT COMPRADA';
    const sub     = isCall ? '<span style="color:#90c090;font-size:.58rem"> especulación alcista</span>'
                           : '<span style="color:#90c090;font-size:.58rem"> cobertura/bajista</span>';
    return `<span style="color:#2ecc71;font-weight:700;font-size:.66rem">${meaning}</span>${sub}`;
  }
  if(f <= 0.35){
    const meaning = isCall ? '🔴 CALL VENDIDA' : '🔴 PUT VENDIDA';
    const sub     = isCall ? '<span style="color:#c09090;font-size:.58rem"> renta/covered call</span>'
                           : '<span style="color:#c09090;font-size:.58rem"> ingreso/bull put</span>';
    return `<span style="color:#e74c3c;font-weight:700;font-size:.66rem">${meaning}</span>${sub}`;
  }
  return `<span style="color:#708090;font-size:.66rem">⚪ MIXTO</span>`;
}
function moneybadge(m){
  if(!m||m==='—') return '';
  const colors={'ATM':'#f0c040','OTM':'#607080','ITM':'#4090c0'};
  const bgs   ={'ATM':'#1e1800','OTM':'#0a1018','ITM':'#081420'};
  const c=colors[m]||'#607080', bg=bgs[m]||'#0a1018';
  return `<span style="font-size:.58rem;padding:1px 4px;border-radius:3px;background:${bg};color:${c};border:1px solid ${c}30;font-weight:700;margin-left:3px">${m}</span>`;
}

function insightText(sym, price, expiries){
  if(!expiries||!expiries.length) return '';
  const first=expiries[0];
  const lvls=(first.levels||[]);
  const above=lvls.filter(l=>l.dist_pct>0  && l.dominant==='call').sort((a,b)=>a.dist_pct-b.dist_pct);
  const below=lvls.filter(l=>l.dist_pct<0  && l.dominant==='put' ).sort((a,b)=>b.dist_pct-a.dist_pct);
  let txt='';
  if(first.max_pain!=null){
    const dir=first.mp_dist>1?'por encima de':first.mp_dist<-1?'por debajo de':'cerca de';
    txt+=`<b>Max Pain ${first.date}:</b> ${first.max_pain>=100?first.max_pain.toFixed(0):first.max_pain.toFixed(2)} — precio ${dir} max pain (${first.mp_dist>=0?'+':''}${(first.mp_dist||0).toFixed(1)}%). Los MMs tienden a defender este nivel antes del vencimiento. `;
  }
  if(above.length) txt+=`<b>Resistencia Gamma:</b> ${above[0].strike>=100?above[0].strike.toFixed(0):above[0].strike.toFixed(2)} (${fmtOI(above[0].c_oi)} calls OI) — los MMs venden delta al subir → techo. `;
  if(below.length) txt+=`<b>Soporte Gamma:</b> ${below[0].strike>=100?below[0].strike.toFixed(0):below[0].strike.toFixed(2)} (${fmtOI(below[0].p_oi)} puts OI) — los MMs compran delta al bajar → piso. `;
  if(first.pc_oi!=null) txt+=`<b>P/C OI:</b> ${first.pc_oi.toFixed(2)} — ${first.pc_oi>1.2?'más puts que calls: posicionamiento defensivo/bajista.':first.pc_oi<0.7?'más calls que puts: optimismo/especulación alcista.':'posicionamiento mixto/neutro.'} `;
  return txt?`<div class="opt-insight">${txt}</div>`:'';
}

async function renderOptions(){
  document.getElementById('root').innerHTML='<div class="mkt-loading">⏳ Cargando cadena de opciones...</div>';
  let j;
  try{ j=await(await fetch('/api/options')).json(); }
  catch(e){ document.getElementById('root').innerHTML=`<div class="mkt-loading">⚠ Error: ${e.message}</div>`; return; }

  const data=j.data||{};
  if(!Object.keys(data).length){
    document.getElementById('root').innerHTML='<div class="mkt-loading">⏳ Calculando datos de opciones por primera vez (~3min)…<br><small style="color:#304050">SPY·SLV·GGAL: CBOE via yfinance — BTC: Deribit</small></div>';
    setTimeout(renderOptions,14000); return;
  }

  let html=`<div class="opt-wrap">
  <div style="padding:0 0 8px;font-size:.68rem;color:#1e3040">
    Actualizado: ${j.updated_at||'—'} &nbsp;·&nbsp;
    <span style="cursor:pointer;color:#2a6aaa" onclick="renderOptions()">↻ Refrescar</span>
    &nbsp;·&nbsp; SPY·SLV·GGAL: CBOE · BTC: Deribit
  </div>

  <div class="opt-insight" style="margin-bottom:12px;font-size:.70rem;color:#507090;line-height:1.6">
    <b>Guía rápida —</b>
    <b>Max Pain:</b> strike donde el total de pérdidas de writers es mínimo; el precio tiende a "pinear" ahí antes del vencimiento. &nbsp;
    <b>Call Wall ▲:</b> strike con máximo OI de calls por encima del precio → resistencia (MMs venden delta al subir). &nbsp;
    <b>Put Wall ▼:</b> máximo OI de puts debajo del precio → soporte (MMs compran al bajar). &nbsp;
    <b>Flow:</b> heurístico bid/ask/last — 🟢 <b>COMPRADA</b> = última operación cerca del ask (comprador inicia) · 🔴 <b>VENDIDA</b> = cerca del bid (vendedor inicia). &nbsp;
    <b>P/C &lt;0.7</b>=alcista · <b>&gt;1.1</b>=bajista. &nbsp; <b>🔵 Mensual · 🟡 Trimestral</b> = vencimientos grandes, mayor poder de pinning.
  </div>

  <div class="opt-ticker-grid">`;

  const TICKER_ORDER=['SPY','BTC','SLV','GGAL'];
  TICKER_ORDER.forEach(sym=>{
    const d=data[sym];
    const displaySym = sym==='BTC'?'BTC':sym;
    if(!d){
      html+=`<div class="opt-card"><div class="opt-sym">${displaySym}</div>
        <div class="opt-insight" style="margin-top:8px">
          ${sym==='BTC'?'Sin datos de Deribit (posible bloqueo de red en Render). Intentar más tarde.':'Sin datos de opciones disponibles.'}
        </div></div>`;
      return;
    }

    const price=d.price;
    const fmtP = p => p==null?'—': p>=1000?p.toFixed(0): p>=100?p.toFixed(2): p.toFixed(4);
    const priceStr=price!=null?`<span class="opt-price-big">${fmtP(price)}</span>`:'';
    const unitNote=d.oi_unit?`<span style="font-size:.60rem;color:#2a4050;margin-left:6px">(OI en ${d.oi_unit})</span>`:'';

    // ── Expiry table ─────────────────────────────────────────────────
    let expTbl=`<div class="opt-section-lbl">Próximos Vencimientos ${unitNote}</div>
    <div class="opt-tbl-scroll"><table class="opt-table"><thead><tr>
      <th>Fecha</th><th class="r">DTE</th>
      <th class="r">Calls OI</th><th class="r">Puts OI</th>
      <th class="r">P/C OI</th><th class="r">Max Pain</th><th class="r">Dist.</th>
    </tr></thead><tbody>`;
    (d.expiries||[]).forEach(ex=>{
      let badge='';
      if(ex.quarterly) badge='<span class="exp-badge exp-q">🟡</span>';
      else if(ex.monthly) badge='<span class="exp-badge exp-m">🔵</span>';
      const pcv=ex.pc_oi;
      const pccls=pcv==null?'oi-tot':pcv<0.7?'oi-call':pcv>1.1?'oi-put':'oi-tot';
      const mpStr=ex.max_pain!=null
        ?`<span class="mp-price">${fmtP(ex.max_pain)}</span>`
        :'<span class="na">—</span>';
      expTbl+=`<tr>
        <td><span class="exp-date">${ex.date}</span>${badge}</td>
        <td class="r"><span class="exp-dte">${ex.dte}d</span></td>
        <td class="r"><span class="oi-call">${fmtOI(ex.c_oi)}</span></td>
        <td class="r"><span class="oi-put">${fmtOI(ex.p_oi)}</span></td>
        <td class="r"><span class="${pccls}">${pcv!=null?pcv.toFixed(2):'—'}</span></td>
        <td class="r">${mpStr}</td>
        <td class="r">${mpDistStr(ex.mp_dist)}</td>
      </tr>`;
    });
    expTbl+='</tbody></table></div>';

    // ── Key OI levels (first expiry) — OI + flow combined per cell ─────
    const firstExp=(d.expiries||[])[0];
    let levelsHtml='';
    if(firstExp && (firstExp.levels||[]).length){
      const maxOI=Math.max(...firstExp.levels.map(l=>l.total_oi),1);
      const hasDeribit=(d.source==='deribit');
      levelsHtml=`<div class="opt-section-lbl">
        Niveles OI — ${firstExp.date}
        <span style="font-weight:400;font-size:.58rem;color:#2a5060">
          &nbsp;▲ resistencia · ▼ soporte${hasDeribit?' · (sin flow: Deribit)':''}
        </span>
      </div>
      <div class="opt-tbl-scroll"><table class="opt-table"><thead><tr>
        <th>Strike</th>
        <th>Tipo</th>
        <th class="r">Calls OI · Flow</th>
        <th class="r">Puts OI · Flow</th>
        <th>Ratio</th>
        <th class="r">Vol · IV</th>
      </tr></thead><tbody>`;
      firstExp.levels.forEach(lv=>{
        const dcls  = lv.dist_pct>0?'d-pos':lv.dist_pct<0?'d-neg':'d-neu';
        const wallLbl = lv.dominant==='call'
          ?'<span style="color:#2ecc71;font-weight:700">▲ Call</span>'
          :'<span style="color:#e74c3c;font-weight:700">▼ Put</span>';
        const cpct  = Math.round(lv.c_oi/(lv.total_oi||1)*100);
        const barW  = Math.round(lv.total_oi/maxOI*70)+12;
        // OI + flow in one cell, 2 lines
        const callCell=`<span class="oi-call">${fmtOI(lv.c_oi)}</span><br><span style="font-size:.60rem">${flowBadge(lv.c_flow,'c')}</span>`;
        const putCell =`<span class="oi-put">${fmtOI(lv.p_oi)}</span><br><span style="font-size:.60rem">${flowBadge(lv.p_flow,'p')}</span>`;
        levelsHtml+=`<tr>
          <td style="white-space:nowrap">
            <b style="font-family:monospace">${lv.strike>=100?lv.strike.toFixed(0):lv.strike.toFixed(2)}</b>
            ${moneybadge(lv.moneyness)}
            <br><span class="${dcls}" style="font-size:.60rem">${lv.dist_pct!=null?(lv.dist_pct>=0?'+':'')+lv.dist_pct.toFixed(1)+'%':'—'}</span>
          </td>
          <td>${wallLbl}</td>
          <td class="r" style="line-height:1.4">${callCell}</td>
          <td class="r" style="line-height:1.4">${putCell}</td>
          <td>
            <div style="display:flex;height:6px;border-radius:3px;overflow:hidden;width:${barW}px;min-width:12px">
              <div style="width:${cpct}%;background:#1a5a28"></div>
              <div style="width:${100-cpct}%;background:#5a1a18"></div>
            </div>
          </td>
          <td class="r" style="font-size:.66rem">
            <span style="color:#405060">${fmtOI(lv.total_vol)}</span>
            <span style="color:#2a4050;margin-left:3px">${lv.c_iv?lv.c_iv.toFixed(0)+'%':'—'}</span>
          </td>
        </tr>`;
      });
      levelsHtml+='</tbody></table></div>';
    }

    html+=`<div class="opt-card">
      <div class="opt-card-hdr">
        <div>
          <div class="opt-sym">${displaySym}</div>
          <div class="opt-name">${d.label}</div>
        </div>
        ${priceStr}${pcChip(d.agg_pc)}
      </div>
      ${expTbl}
      ${levelsHtml}
      ${insightText(sym, price, d.expiries||[])}
    </div>`;
  });

  html+=`</div></div>`;
  document.getElementById('root').innerHTML=html;
}

/* ── fetch ── */
// FIX: tabs que usan renderAll() — market/mining/options tienen sus propios renderers
const STOCK_TABS = new Set(['prices','valuation','growth','technical','analysts']);
let _fetchErrCount=0;
async function refresh(){
  let fetchOk=false;
  try{
    const ctrl=new AbortController();
    const tid=setTimeout(()=>ctrl.abort(),8000);
    const j=await(await fetch('/api/stocks',{signal:ctrl.signal})).json();
    clearTimeout(tid);
    fetchOk=true;
    _fetchErrCount=0;
    allData=markTopPicks(j.data);
    const liveN=j.data.filter(s=>s.source==='live').length;
    const lel=document.getElementById('live-count');
    if(liveN>0){lel.textContent=`🟢 LIVE ${liveN}/${j.data.length}`;lel.style.display='';}
    else lel.style.display='none';
    document.getElementById('updated').textContent=j.updated_at;
    // FIX: solo llamar renderAll() para tabs que están en STOCK_TABS
    // (mining/options/market tienen sus propios render functions — llamar renderAll()
    //  en ellas crashea con TypeError porque TABS[tab] es undefined)
    if(STOCK_TABS.has(activeTab)) renderAll();
  }catch(e){
    if(!fetchOk){
      _fetchErrCount++;
      const el=document.getElementById('updated');
      if(el) el.textContent=`⚠ Sin conexión (${_fetchErrCount}x) — reintentando...`;
      if(_fetchErrCount>=10) window.location.reload();
    } else {
      console.warn('refresh render error:',e.message);
    }
  }
}

refresh();
setInterval(refresh,POLL);
setInterval(()=>{ if(activeTab==='market') renderMarket(); }, 60000);
setInterval(()=>{ if(activeTab==='mining') renderMining(); }, 120000);
setInterval(()=>{ if(activeTab==='options') renderOptions(); }, 180000);
</script>

<!-- Feature Pack: Portfolio Simulator | Position Size Calculator | Export CSV | Ticker Tape -->
<script>
(function() {
  'use strict';
  if (document.getElementById('mf-css')) return;

  var s = document.createElement('style');
  s.id = 'mf-css';
  s.textContent = [
    '.fab-c{position:fixed;bottom:20px;right:20px;z-index:9990;display:flex;flex-direction:column-reverse;gap:12px;align-items:center}',
    '.fab-b{width:50px;height:50px;border-radius:50%;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:22px;box-shadow:0 4px 15px rgba(0,0,0,.4);transition:transform .2s}',
    '.fab-b:hover{transform:scale(1.15)}',
    '.fab-b .ft{position:absolute;right:60px;background:#1a2d45;color:#dce8f0;padding:6px 12px;border-radius:8px;font-size:12px;white-space:nowrap;opacity:0;pointer-events:none;transition:opacity .2s}',
    '.fab-b:hover .ft{opacity:1}',
    '.mo{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:9998;display:none}',
    '.mo.op{display:block}',
    '.mp{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);max-width:95vw;max-height:82vh;background:#0d1b2a;border:1px solid #1e3a5f;border-radius:16px;z-index:10000;display:none;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.7);overflow:hidden}',
    '.mp.op{display:flex}',
    '.mh{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid #1e3a5f;background:linear-gradient(135deg,#142030,#1a2d45)}',
    '.mh h2{font-size:17px;margin:0}',
    '.mc{background:none;border:none;color:#8899aa;font-size:22px;cursor:pointer}',
    '.mc:hover{color:#ff6b6b}',
    '.mb{padding:18px 20px;overflow-y:auto;flex:1}',
    '.pi{background:#0a1520;border:1px solid #1e3a5f;color:#dce8f0;padding:8px 12px;border-radius:8px;font-size:13px}',
    '.pi:focus{border-color:#00c9ff;outline:none}',
    '.pa{background:linear-gradient(135deg,#00c9ff,#92fe9d);color:#0a1520;border:none;padding:8px 16px;border-radius:8px;cursor:pointer;font-weight:700;font-size:13px}',
    '.pt{width:100%;border-collapse:collapse}',
    '.pt th{text-align:left;padding:8px;font-size:11px;text-transform:uppercase;color:#5a7a96;border-bottom:1px solid #1e3a5f}',
    '.pt td{padding:8px;font-size:13px;border-bottom:1px solid #0f2234}',
    '.pg{color:#00e676!important}',
    '.pl{color:#ff5252!important}',
    '.pr{background:none;border:none;color:#ff5252;cursor:pointer;font-size:15px}',
    '.ps{display:flex;justify-content:space-around;padding:14px 20px;border-top:1px solid #1e3a5f;background:#0a1520}',
    '.pst{text-align:center}',
    '.psl{font-size:10px;text-transform:uppercase;color:#5a7a96}',
    '.psv{font-size:18px;font-weight:700;margin-top:2px}',
    '.cg{display:grid;grid-template-columns:1fr 1fr;gap:14px}',
    '.cf label{display:block;font-size:11px;color:#5a7a96;text-transform:uppercase;margin-bottom:4px}',
    '.cf input{width:100%;background:#0a1520;border:1px solid #1e3a5f;color:#dce8f0;padding:10px 12px;border-radius:8px;font-size:14px;box-sizing:border-box}',
    '.cf input:focus{border-color:#f093fb;outline:none}',
    '.cr{margin-top:18px;padding:16px;background:#0a1520;border-radius:12px;border:1px solid #1e3a5f}',
    '.crg{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:10px}',
    '.cri{text-align:center}',
    '.cri .lb{font-size:10px;text-transform:uppercase;color:#5a7a96}',
    '.cri .vl{font-size:18px;font-weight:700;margin-top:2px;color:#f093fb}',
    '.et{position:fixed;bottom:60px;left:50%;transform:translateX(-50%);background:#1a2d45;color:#92fe9d;padding:12px 24px;border-radius:10px;font-size:14px;z-index:99999;opacity:0;transition:opacity .3s;border:1px solid #00c9ff}',
    '.et.sh{opacity:1}'
  ].join('\\n');
  document.head.appendChild(s);

  var fc = document.createElement('div');
  fc.className = 'fab-c';
  fc.innerHTML = '<button class="fab-b" id="fb-p" style="background:linear-gradient(135deg,#00c9ff,#92fe9d)"><span class="ft">Portfolio Simulator</span>P</button>' +
    '<button class="fab-b" id="fb-c" style="background:linear-gradient(135deg,#f093fb,#f5576c)"><span class="ft">Position Sizer</span>C</button>' +
    '<button class="fab-b" id="fb-e" style="background:linear-gradient(135deg,#ffd86f,#fc6262)"><span class="ft">Exportar CSV</span>E</button>';
  document.body.appendChild(fc);

  var po = document.createElement('div'); po.className = 'mo'; po.id = 'po'; document.body.appendChild(po);
  var pp = document.createElement('div'); pp.className = 'mp'; pp.id = 'pp'; pp.style.width = '720px';
  pp.innerHTML = '<div class="mh"><h2 style="color:#00c9ff">P Portfolio Simulator</h2><button class="mc" id="pc">&times;</button></div>' +
    '<div class="mb"><div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">' +
    '<input class="pi" type="text" id="pt" placeholder="Ticker (ej: AAPL)" style="width:120px">' +
    '<input class="pi" type="number" id="pq" placeholder="Acciones" style="width:100px" min="1">' +
    '<input class="pi" type="number" id="pb" placeholder="Precio compra $" style="width:140px" step="0.01">' +
    '<button class="pa" id="padd">+ Agregar</button></div>' +
    '<table class="pt"><thead><tr><th>Ticker</th><th>Cant.</th><th>Compra</th><th>Actual</th><th>P&L $</th><th>P&L %</th><th>Valor</th><th></th></tr></thead>' +
    '<tbody id="ptb"><tr><td colspan="8" style="text-align:center;color:#5a7a96;padding:30px">Agrega acciones para simular tu portfolio</td></tr></tbody></table></div>' +
    '<div class="ps">' +
    '<div class="pst"><div class="psl">Invertido</div><div class="psv" id="si">$0</div></div>' +
    '<div class="pst"><div class="psl">Valor Actual</div><div class="psv" id="sv">$0</div></div>' +
    '<div class="pst"><div class="psl">P&L Total</div><div class="psv" id="sp">$0</div></div>' +
    '<div class="pst"><div class="psl">Retorno</div><div class="psv" id="sr">0%</div></div></div>';
  document.body.appendChild(pp);

  var co = document.createElement('div'); co.className = 'mo'; co.id = 'co'; document.body.appendChild(co);
  var cp = document.createElement('div'); cp.className = 'mp'; cp.id = 'cp'; cp.style.width = '520px';
  cp.innerHTML = '<div class="mh"><h2 style="color:#f093fb">C Position Size Calculator</h2><button class="mc" id="cc">&times;</button></div>' +
    '<div class="mb"><div class="cg">' +
    '<div class="cf"><label>Capital Total ($)</label><input type="number" id="c1" value="10000" step="100"></div>' +
    '<div class="cf"><label>Riesgo por Trade (%)</label><input type="number" id="c2" value="2" step="0.5"></div>' +
    '<div class="cf"><label>Precio Entrada ($)</label><input type="number" id="c3" placeholder="ej: 150.00" step="0.01"></div>' +
    '<div class="cf"><label>Stop Loss ($)</label><input type="number" id="c4" placeholder="ej: 145.00" step="0.01"></div></div>' +
    '<div class="cr" id="cres" style="display:none"><div style="font-size:13px;color:#8899aa;text-transform:uppercase;letter-spacing:1px">Resultado</div>' +
    '<div class="crg"><div class="cri"><div class="lb">Acciones</div><div class="vl" id="r1">—</div></div>' +
    '<div class="cri"><div class="lb">Monto a Invertir</div><div class="vl" id="r2">—</div></div>' +
    '<div class="cri"><div class="lb">Riesgo $</div><div class="vl" id="r3">—</div></div></div>' +
    '<div class="crg" style="margin-top:12px"><div class="cri"><div class="lb">R:R 2:1 Target</div><div class="vl" id="r4" style="color:#00e676">—</div></div>' +
    '<div class="cri"><div class="lb">R:R 3:1 Target</div><div class="vl" id="r5" style="color:#00e676">—</div></div>' +
    '<div class="cri"><div class="lb">% del Capital</div><div class="vl" id="r6">—</div></div></div></div></div>';
  document.body.appendChild(cp);

  var toast = document.createElement('div');
  toast.className = 'et'; toast.id = 'toast';
  toast.textContent = 'CSV exportado exitosamente';
  document.body.appendChild(toast);

  window._pf = [];

  window.findPrice = function(tk) {
    var tables = document.querySelectorAll('.table-wrap table');
    var up = tk.toUpperCase();
    for (var t = 0; t < tables.length; t++) {
      var rows = tables[t].querySelectorAll('tr');
      for (var r = 0; r < rows.length; r++) {
        var tds = rows[r].querySelectorAll('td');
        if (tds.length < 6) continue;
        var ct = tds[2] ? tds[2].textContent.trim() : '';
        if (ct.toUpperCase().indexOf(up) === 0) {
          var pt = tds[5] ? tds[5].textContent.trim() : '';
          var p = parseFloat(pt.replace(/[^0-9.]/g, ''));
          if (p > 0) return p;
        }
      }
    }
    return null;
  };

  function tog(pid, oid) {
    document.getElementById(pid).classList.toggle('op');
    document.getElementById(oid).classList.toggle('op');
  }

  document.getElementById('fb-p').onclick = function() { tog('pp', 'po'); upPF(); };
  document.getElementById('po').onclick = function() { tog('pp', 'po'); };
  document.getElementById('pc').onclick = function() { tog('pp', 'po'); };

  window.upPF = function() {
    var tb = document.getElementById('ptb');
    if (!tb) return;
    if (!window._pf.length) {
      tb.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#5a7a96;padding:30px">Agrega acciones para simular tu portfolio</td></tr>';
      document.getElementById('si').textContent = '$0';
      document.getElementById('sv').textContent = '$0';
      document.getElementById('sp').textContent = '$0';
      document.getElementById('sr').textContent = '0%';
      return;
    }
    var ti = 0, tv = 0;
    tb.innerHTML = window._pf.map(function(p, i) {
      var cur = findPrice(p.t) || p.b;
      var inv = p.q * p.b, val = p.q * cur, pnl = val - inv, pct = (cur - p.b) / p.b * 100;
      ti += inv; tv += val;
      var c = pnl >= 0 ? 'pg' : 'pl', sg = pnl >= 0 ? '+' : '';
      return '<tr><td><b>' + p.t + '</b></td><td>' + p.q + '</td><td>$' + p.b.toFixed(2) + '</td><td>$' + cur.toFixed(2) + '</td><td class="' + c + '">' + sg + '$' + pnl.toFixed(2) + '</td><td class="' + c + '">' + sg + pct.toFixed(2) + '%</td><td>$' + val.toFixed(2) + '</td><td><button class="pr" onclick="rmPF(' + i + ')">x</button></td></tr>';
    }).join('');
    var tp = tv - ti, tr = ti > 0 ? tp / ti * 100 : 0, c = tp >= 0 ? 'pg' : 'pl';
    document.getElementById('si').textContent = '$' + ti.toLocaleString('en-US', { minimumFractionDigits: 2 });
    document.getElementById('sv').textContent = '$' + tv.toLocaleString('en-US', { minimumFractionDigits: 2 });
    document.getElementById('sp').textContent = (tp >= 0 ? '+$' : '-$') + Math.abs(tp).toLocaleString('en-US', { minimumFractionDigits: 2 });
    document.getElementById('sp').className = 'psv ' + c;
    document.getElementById('sr').textContent = (tr >= 0 ? '+' : '') + tr.toFixed(2) + '%';
    document.getElementById('sr').className = 'psv ' + c;
  };

  document.getElementById('padd').onclick = function() {
    var t = document.getElementById('pt').value.toUpperCase().trim();
    var q = parseInt(document.getElementById('pq').value);
    var b = parseFloat(document.getElementById('pb').value);
    if (!t || !q || !b) return;
    window._pf.push({ t: t, q: q, b: b });
    document.getElementById('pt').value = '';
    document.getElementById('pq').value = '';
    document.getElementById('pb').value = '';
    upPF();
  };
  window.rmPF = function(i) { window._pf.splice(i, 1); upPF(); };

  document.getElementById('fb-c').onclick = function() { tog('cp', 'co'); };
  document.getElementById('co').onclick = function() { tog('cp', 'co'); };
  document.getElementById('cc').onclick = function() { tog('cp', 'co'); };

  function calc() {
    var cap = parseFloat(document.getElementById('c1').value);
    var rp = parseFloat(document.getElementById('c2').value);
    var en = parseFloat(document.getElementById('c3').value);
    var sl = parseFloat(document.getElementById('c4').value);
    if (!cap || !rp || !en || !sl || en === sl) { document.getElementById('cres').style.display = 'none'; return; }
    var ra = cap * (rp / 100), rps = Math.abs(en - sl), sh = Math.floor(ra / rps), am = sh * en, pc = am / cap * 100;
    var isL = en > sl, t2 = isL ? en + rps * 2 : en - rps * 2, t3 = isL ? en + rps * 3 : en - rps * 3;
    document.getElementById('cres').style.display = 'block';
    document.getElementById('r1').textContent = sh;
    document.getElementById('r2').textContent = '$' + am.toFixed(2);
    document.getElementById('r3').textContent = '$' + ra.toFixed(2);
    document.getElementById('r4').textContent = '$' + t2.toFixed(2);
    document.getElementById('r5').textContent = '$' + t3.toFixed(2);
    document.getElementById('r6').textContent = pc.toFixed(1) + '%';
  }
  ['c1', 'c2', 'c3', 'c4'].forEach(function(id) {
    document.getElementById(id).addEventListener('input', calc);
  });

  document.getElementById('fb-e').onclick = function() {
    var rows = document.querySelectorAll('.table-wrap tr');
    var csv = [];
    rows.forEach(function(row) {
      var cells = row.querySelectorAll('th,td');
      if (cells.length >= 5) {
        csv.push(Array.from(cells).map(function(c) {
          return '"' + c.textContent.trim().replace(/"/g, '""') + '"';
        }).join(','));
      }
    });
    if (csv.length < 2) return;
    var blob = new Blob([csv.join('\\n')], { type: 'text/csv' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'stocks_' + new Date().toISOString().slice(0, 10) + '.csv';
    a.click();
    var t = document.getElementById('toast');
    t.classList.add('sh');
    setTimeout(function() { t.classList.remove('sh'); }, 2500);
  };

  var host = document.createElement('div');
  host.id = 'tape-h';
  host.setAttribute('style', 'position:fixed !important;bottom:0 !important;left:0 !important;right:0 !important;height:38px !important;z-index:999999 !important;');
  document.documentElement.appendChild(host);
  var shadow = host.attachShadow({ mode: 'open' });

  function renderTape() {
    var stocks = [], seen = {};
    var tables = document.querySelectorAll('.table-wrap table');
    for (var t = 0; t < tables.length; t++) {
      var rows = tables[t].querySelectorAll('tr');
      for (var r = 0; r < rows.length; r++) {
        var tds = rows[r].querySelectorAll('td');
        if (tds.length < 8) continue;
        var tk = tds[2] ? tds[2].textContent.trim().split(' ')[0].split('\\n')[0] : '';
        if (!tk || seen[tk]) continue;
        seen[tk] = true;
        var price = tds[5] ? tds[5].textContent.trim() : '';
        var chg = tds[7] ? tds[7].textContent.trim() : '';
        var up = !chg.includes('-');
        if (price.indexOf('$') === 0) stocks.push({ t: tk, p: price, c: chg, u: up });
      }
    }
    if (!stocks.length) return;
    var all = stocks.concat(stocks).concat(stocks);
    var sp = '';
    all.forEach(function(s) {
      var col = s.u ? '#00e676' : '#ff5252', arr = s.u ? '▲' : '▼';
      sp += '<span style="display:inline-block;margin-right:30px;font-size:13px;font-family:Segoe UI,sans-serif;"><b style="color:#00c9ff;">' + s.t + '</b> <span style="color:#dce8f0;">' + s.p + '</span> <span style="color:' + col + ';">' + arr + ' ' + s.c + '</span></span>';
    });
    shadow.innerHTML = '<style>@keyframes sc{0%{transform:translateX(0)}100%{transform:translateX(-66.66%)}}</style>' +
      '<div style="width:100%;height:38px;background:linear-gradient(90deg,#0a1420,#152535,#0a1420);border-top:1px solid #1e3a5f;display:flex;align-items:center;overflow:hidden;">' +
      '<div style="display:inline-block;white-space:nowrap;animation:sc 60s linear infinite;">' + sp + '</div></div>';
  }
  renderTape();
  setInterval(renderTape, 15000);

  console.log('Stock Dashboard Feature Pack loaded!');
})();
</script>
</body>
</html>"""

if __name__=='__main__':
    print("="*58)
    print("  USA Stock Dashboard  —  Finnhub Real-Time")
    print(f"  Puerto: {PORT}")
    print(f"  {len(ALL_TICKERS)} tickers  |  WS tiempo real  |  refresh {POLL_MS//1000}s")
    print("="*58)
    # Todos los datos cargan en background — Flask arranca PRIMERO
    # para que Render/cloud reciba el health-check sin timeout
    threading.Thread(target=load_fundamentals, daemon=True).start()
    time.sleep(15)  # stagger startup to avoid memory spike
    threading.Thread(target=load_technicals,   daemon=True).start()
    time.sleep(10)
    threading.Thread(target=load_sparklines,   daemon=True).start()
    time.sleep(10)
    threading.Thread(target=load_market_data,  daemon=True).start()
    time.sleep(10)
    threading.Thread(target=load_mining_data,  daemon=True).start()
    time.sleep(15)
    threading.Thread(target=load_options_data, daemon=True).start()
    threading.Thread(target=fundamentals_loop, daemon=True).start()
    threading.Thread(target=technicals_loop,   daemon=True).start()
    threading.Thread(target=sparklines_loop,   daemon=True).start()
    threading.Thread(target=market_loop,       daemon=True).start()
    threading.Thread(target=mining_loop,       daemon=True).start()
    threading.Thread(target=options_loop,      daemon=True).start()
    threading.Thread(target=start_ws,          daemon=True).start()
    # Solo abre el navegador si estamos corriendo localmente
    if not os.environ.get("PORT"):
        threading.Timer(3.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    print(f"Servidor iniciado en puerto {PORT}. Los datos cargan en ~30s en segundo plano.")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
