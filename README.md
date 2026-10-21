# binance-bot

Tools sederhana: **scanner sinyal LONG + SHORT** + **auto-trading Binance USDT-M Futures**.

Dirancang ulang dari nol — fokus engine sinyal bidirectional, bukan port penuh bot lama.

## Fitur

- **LONG dan SHORT** (default `BULLISH_ONLY=False`)
- Setup 15m: EMA 9/21/50, RSI, MACD, volume
- **Noise filter**: Efficiency Ratio, doji, direction flips, DI conflict, overextension
- **MTF weighted**: hard gate 1h, skor weighted 1h/30m/15m/5m ≥ 0.70
- **BTC align** opsional (15m)
- Freshness vs entry zone
- Quality score ≥ `MIN_QUALITY`
- Auto entry LIMIT/MARKET + STOP_MARKET SL + TAKE_PROFIT_MARKET TP1
- Mode: `paper` | `testnet` | `live`

## Install

```bash
cd binance-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — isi API key testnet
```

Load env (opsional):

```bash
export $(grep -v '^#' .env | xargs)
```

## Perintah

```bash
# Satu kali scan (public data, API key tidak wajib)
python main.py scan

# Lihat sinyal fresh
python main.py signals

# Auto-trade dari sinyal (butuh key testnet/live)
python main.py trade

# Scan + trade sekali
python main.py run

# Loop terus (default 15 menit)
python main.py loop
python main.py loop --interval 600

# Akun
python main.py balance
python main.py positions

# Engine SPOT (swing, LONG-only, tanpa SL)
python main.py spot-scan
python main.py spot-trade
python main.py spot-loop
python main.py spot-loop --interval 900

# Setting runtime (berlaku siklus berikutnya, tanpa restart)
python main.py settings
python main.py set leverage 20
python main.py set tp_roi_pct 30
python main.py set margin_usd 100
```

## Engine signal (ringkas)

```
volume 24h
  → 15m BULLISH/BEARISH WATCH (strength ≥ 3)
  → regime brake (RANGING = no-trade zone, REGIME_BLOCK_RANGING)
  → noise filter
  → MTF (1h hard, weighted score ≥ 0.70 dari leg tren EMA 1h/30m/15m/5m)
  → BTC align (opsional: vote 15m + tren 1h tidak boleh melawan)
  → freshness live price
  → quality ≥ MIN_QUALITY
  → SIGNAL LONG atau SHORT
```

## Dua engine: futures + spot

| | Futures (scalping harian) | Spot (swing) |
|---|---|---|
| Trigger | 15m | 1h |
| MTF | 1h/30m/15m/5m (hard 1h) | 4h/1h/30m/15m (hard 4h) |
| Arah | LONG + SHORT | LONG only (spot tidak bisa short) |
| SL | STOP_MARKET via Algo Order | TIDAK ADA (spot tidak liquidasi) |
| Exit | SL / TP1 exchange + profit-take bot | TP1 bot-side full close |
| Sizing | margin fixed / risiko | USDT fixed per beli |
| State | executor_state.json | spot_state.json |
| Sinyal | signals.db | spot_signals.db |

Spot memakai struktur pasar HH/HL sebagai gate + pattern candle
(bullish engulfing, hammer) sebagai bonus quality. Kunci spot
TERPISAH dari kunci futures: kunci spot testnet dari
testnet.binance.vision (`SPOT_API_KEY`/`SPOT_API_SECRET`), bukan
kunci futures testnet. Batas v1 spot: full close di TP1 saja (TP2
tercatat untuk partial-exit berikutnya), belum ada exit reversal,
fee 0,1% tidak dimodelkan.

## Risk

- Default **testnet**. Live hanya jika `TRADE_MODE=live` **dan** `LIVE_CONFIRM=YES`.
- Size = `RISK_PCT%` equity / jarak entry−stop.
- Max posisi terbuka: `MAX_OPEN_POSITIONS`.
- Margin default **ISOLATED** (`MARGIN_MODE`). Gagal set margin/leverage = entry dibatalkan.
- SL/TP dipasang **setelah entry terisi** (LIMIT ditunggu/dipoll; belum terisi → dicatat
  sebagai pending dan direkonsiliasi di pass berikutnya; pending lewat `ORDER_TTL_SEC`
  dibatalkan). Jika SL gagal dipasang setelah fill (3x retry), posisi **ditutup darurat**
  via market reduce-only — tidak ada posisi telanjang yang dibiarkan.
- **Kill switch**: buat file `KILL` di folder `data/` → semua entry baru terblokir.
- **Batas rugi harian**: entry terblokir bila equity turun > `DAILY_LOSS_CAP_PCT%`
  dari equity awal hari (dicatat di `executor_state.json`).
- Entry diskip bila notional (qty × harga) di bawah MIN_NOTIONAL exchange /
  `MIN_NOTIONAL_USDT`.

## File data

```
data/signals.db      # sinyal
data/state.json      # ringkasan scan terakhir
data/executor_state.json
data/bot.log
```

## Konfigurasi penting

| Env / config | Default | Arti |
|--------------|---------|------|
| `TRADE_MODE` | testnet | paper / testnet / live |
| `BULLISH_ONLY` | False | False = LONG+SHORT |
| `MIN_QUALITY` | 62 | gerbang kualitas |
| `BTC_ALIGN` | 1 | arah BTC harus sama |
| `RISK_PCT` | 1.0 | % equity per trade |
| `MAX_LEVERAGE` | 5 | leverage |
| `MARGIN_MODE` | ISOLATED | ISOLATED / CROSSED |
| `MIN_NOTIONAL_USDT` | 5 | notional minimum per entry |
| `DAILY_LOSS_CAP_PCT` | 10 | blokir entry bila equity harian turun melewati % ini |
| `ORDER_TTL_SEC` | 1800 | umur maks order LIMIT pending sebelum dibatalkan |
| `UNIVERSE` | top | top volume atau list |

## Disclaimer

Ini tools edukasi/otomasi. Trading futures berisiko tinggi. Uji di **testnet** dulu. Tidak ada jaminan profit.
