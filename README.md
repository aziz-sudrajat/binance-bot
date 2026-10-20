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
