"""
dashboard.py  —  Real-Time USA Stock Dashboard (Finnhub WebSocket)
Uso:  pip install flask finnhub-python websocket-client
      python dashboard.py  →  http://localhost:5050
"""
import threading, time, json, webbrowser, os
from flask import Flask, jsonify, render_template_string
from datetime import datetime, timedelta
import finnhub, websocket, yfinance as yf
import pandas as pd

API_KEY        = os.environ.get("FINNHUB_API_KEY", "d764qhpr01qm4b7sv75gd764qhpr01qm4b7sv760")
PORT           = int(os.environ.get("PORT", 5050))
POLL_MS        = 3000
FUND_REFRESH_S = 300

REQUESTED   = ["META","GOOGL","MSFT","NVDA","ORCL","MU","NFLX","SPOT","TSLA","NU","PAGS","STNE"]
RECOMMENDED = ["AMZN","AMD","V","ASML","JPM","BABA","MELI","ADBE","AVGO","QCOM","CRM","MRVL","TXN"]
AI_GROWTH   = ["NBIS","ASTS","ONDS","IREN","RKLB","PLTR","ARM","SMCI","CRWD","NET","ANET","IONQ"]
ALL_TICKERS = REQUESTED + RECOMMENDED + AI_GROWTH

_prices     = {}
_info       = {}
_tech       = {}
_sparklines = {}   # ticker -> [price, price, ...]
_lock       = threading.Lock()
fc = finnhub.Client(api_key=API_KEY)

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
                }
            print(f"  📊 {ticker:6s}  RSI-D={rsi_d:.0f}  EMA200={'%+.1f%%'%pct_e200 if pct_e200 else '?'}  {'🟢' if signal=='buy' else '🔴' if signal=='sell' else '⚪'}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  ✗ tech {ticker}: {e}")

def technicals_loop():
    while True:
        time.sleep(900)
        print(f"\n[{time.strftime('%H:%M:%S')}] Refrescando indicadores técnicos...")
        load_technicals()

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
        time.sleep(1800)   # refresh cada 30 min
        print(f"\n[{time.strftime('%H:%M:%S')}] Refrescando sparklines...")
        load_sparklines()

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
    print("WS cerrado, reconectando..."); time.sleep(5); start_ws()
def on_open(ws_app):
    print(f"[{time.strftime('%H:%M:%S')}] WS conectado, suscribiendo {len(ALL_TICKERS)} tickers...")
    for sym in ALL_TICKERS:
        ws_app.send(json.dumps({"type":"subscribe","symbol":sym}))
    print("Suscripciones activas ✓")

def start_ws():
    ws_app = websocket.WebSocketApp(f"wss://ws.finnhub.io?token={API_KEY}",
        on_message=on_message,on_error=on_error,on_close=on_close,on_open=on_open)
    ws_app.run_forever(ping_interval=30,ping_timeout=10)

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
                'sparkline':     _sparklines.get(ticker, []),
            })
    return jsonify({'data':result,'updated_at':time.strftime("%d/%m/%Y  %H:%M:%S")})

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
  el.classList.add('active'); renderAll();
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

/* ── fetch ── */
async function refresh(){
  try{
    const j=await(await fetch('/api/stocks')).json();
    allData=markTopPicks(j.data);
    const liveN=j.data.filter(s=>s.source==='live').length;
    const lel=document.getElementById('live-count');
    if(liveN>0){lel.textContent=`🟢 LIVE ${liveN}/${j.data.length}`;lel.style.display='';}
    else lel.style.display='none';
    document.getElementById('updated').textContent=j.updated_at;
    renderAll();
  }catch(e){document.getElementById('updated').textContent='Error: '+e.message;}
}

refresh();
setInterval(refresh,POLL);
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
    threading.Thread(target=load_technicals,   daemon=True).start()
    threading.Thread(target=load_sparklines,   daemon=True).start()
    threading.Thread(target=fundamentals_loop, daemon=True).start()
    threading.Thread(target=technicals_loop,   daemon=True).start()
    threading.Thread(target=sparklines_loop,   daemon=True).start()
    threading.Thread(target=start_ws,          daemon=True).start()
    # Solo abre el navegador si estamos corriendo localmente
    if not os.environ.get("PORT"):
        threading.Timer(3.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    print(f"Servidor iniciado en puerto {PORT}. Los datos cargan en ~30s en segundo plano.")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
