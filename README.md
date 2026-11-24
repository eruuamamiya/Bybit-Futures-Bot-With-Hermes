# Bybit Futures Bot dengan Hermes (2 Profile)

Bot trading Bybit Linear Futures yang dijalankan satu instance **Hermes Agent** dengan dua profile:

| Profile | Tugas | Tools |
|---|---|---|
| `analyst` | Menilai kandidat dari data indikator, lalu membalas JSON (LONG / SHORT / SKIP) | tidak ada |
| `trader` | Menjalankan scan, meminta keputusan ke `analyst`, memvalidasi, lalu eksekusi order | `terminal`, `delegate` |

Semua perhitungan indikator dan order ada di `start.py` (berbasis CCXT), jadi AI tidak menghitung atau menebak harga sendiri.

## Alur

```
cron (tiap 15 menit)
  -> profile trader
       1. start.py scan             -> cek saldo, posisi aktif, kandidat
       2. delegate ke analyst       -> keputusan JSON
       3. validasi (confidence, margin, leverage)
       4. start.py execute          -> market order + TP/SL native Bybit

cron (tiap 15 menit, tanpa AI)
  -> start.py manage                -> geser SL ke break-even saat profit tercapai
```

## Default strategi

| Parameter | Nilai |
|---|---|
| Modal per posisi (margin) | 3.5 USDT |
| Leverage | 5x Isolated |
| Take Profit | 15% dari harga |
| Stop Loss | 5% dari harga |
| Break-even | SL digeser ke harga masuk setelah profit 7.5% dari harga |
| Maks posisi bersamaan | 2 |
| Sinyal | EMA9/EMA21, RSI, volume di 1H, filter tren EMA50 di 4H, filter funding rate |
| Watchlist | SOL, SUI, DOGE, XRP, 1000PEPE, WIF |

Semua nilai ini bisa diubah di bagian atas `start.py`. Perhatikan bahwa `execute` menolak order jika leverage lebih besar dari `MAX_LEVERAGE` (default 10) atau jika `SL x leverage` lebih dari 50.

---

## Tahap 1: Siapkan folder dan dependensi

Login ke VPS, lalu buat folder proyek dan folder kedua profile:

```bash
mkdir -p ~/bybit-bot
mkdir -p ~/.hermes/profiles/analyst
mkdir -p ~/.hermes/profiles/trader

pip install ccxt requests
```

Taruh `start.py` di `~/bybit-bot/start.py`.

## Tahap 2: Simpan API key

Simpan key di `~/.bashrc` dan `~/.hermes/.env` agar terbaca oleh CLI maupun cron Hermes:

```bash
cat << 'EOF' >> ~/.bashrc
export LLM_API_KEY="ISI_API_KEY_PROVIDER_LLM"
export BYBIT_API_KEY="ISI_API_KEY_BYBIT"
export BYBIT_SECRET_KEY="ISI_SECRET_KEY_BYBIT"
EOF
source ~/.bashrc

cat << 'EOF' >> ~/.hermes/.env
LLM_API_KEY="ISI_API_KEY_PROVIDER_LLM"
BYBIT_API_KEY="ISI_API_KEY_BYBIT"
BYBIT_SECRET_KEY="ISI_SECRET_KEY_BYBIT"
EOF
```

Pengaturan API key di Bybit:

- Centang hanya izin **Orders** dan **Positions**. Jangan beri izin withdraw.
- Aktifkan **IP Whitelist** dengan IP VPS kamu.
- Set **Position Mode** ke **One-Way Mode**.

## Tahap 3: Profile 1 (`analyst`)

Profile ini hanya menganalisis dan membalas JSON, jadi semua tools dimatikan.

`~/.hermes/profiles/analyst/config.yaml`:

```bash
cat << 'EOF' > ~/.hermes/profiles/analyst/config.yaml
providers:
  llm:
    name: LLM Provider
    base_url: https://api.openai.com/v1
    key_env: LLM_API_KEY

model:
  provider: llm
  model_name: gpt-4o
  temperature: 0.1
  timeout: 180

tools:
  enabled_tools: []
EOF
```

`~/.hermes/profiles/analyst/SOUL.md`:

```bash
cat << 'EOF' > ~/.hermes/profiles/analyst/SOUL.md
Kamu adalah analis teknikal dan risiko Bybit Linear Futures.
Tugasmu menganalisis indikator teknikal, volume spike, tren 4H, dan funding rate yang diberikan, lalu memutuskan apakah layak entry LONG, SHORT, atau SKIP.

ATURAN WAJIB:
1. Balas HANYA dengan satu objek JSON mentah yang valid. Dilarang memakai blok kode markdown, sapaan, atau penjelasan di luar JSON.
2. Margin per posisi 3.5 USDT dengan leverage 5x Isolated.
3. Take Profit 15% dari harga entry.
4. Stop Loss 5% dari harga entry, berlawanan arah posisi.
5. Keluarkan LONG atau SHORT hanya jika confidence >= 78 dan EMA, RSI, volume spike, serta tren 4H searah. Jika ragu atau sideways, wajib SKIP.

SKEMA JSON WAJIB:
{
  "decision": "LONG" | "SHORT" | "SKIP",
  "symbol": "PAIRUSDT",
  "leverage": 5,
  "margin_usdt": 3.5,
  "tp_pct_price": 15,
  "sl_pct_price": 5,
  "confidence": 82,
  "reason": "Alasan singkat maksimal 15 kata dalam bahasa Indonesia"
}
EOF
```

## Tahap 4: Profile 2 (`trader`)

Profile ini punya akses `terminal` untuk menjalankan `start.py` dan `delegate` untuk memanggil `analyst`.

`~/.hermes/profiles/trader/config.yaml`:

```bash
cat << 'EOF' > ~/.hermes/profiles/trader/config.yaml
providers:
  llm:
    name: LLM Provider
    base_url: https://api.openai.com/v1
    key_env: LLM_API_KEY

model:
  provider: llm
  model_name: gpt-4o-mini
  temperature: 0.1

tools:
  enabled_tools:
    - terminal
    - delegate
EOF
```

`~/.hermes/profiles/trader/SOUL.md`:

```bash
cat << 'EOF' > ~/.hermes/profiles/trader/SOUL.md
Kamu adalah eksekutor otonom Bybit Linear Futures. Kamu dilarang menebak harga atau menghitung indikator sendiri. Seluruh data dan eksekusi dijalankan lewat /home/hermes/bybit-bot/start.py.

ALUR KERJA SETIAP SIKLUS:
1. Jalankan `python3 /home/hermes/bybit-bot/start.py scan` lewat terminal untuk mengecek saldo USDT, posisi aktif (maksimal 2), dan kandidat pair.
2. Jika status bukan CANDIDATE_FOUND, akhiri siklus tanpa melakukan apa pun.
3. Jika CANDIDATE_FOUND, delegasikan data kandidat ke sub-agent profile `analyst` untuk meminta keputusan JSON.
4. Validasi hasil JSON dari analyst:
   - decision harus LONG atau SHORT dan confidence >= 78.
   - margin_usdt <= 3.5 dan leverage == 5.
   - Jika SKIP atau confidence < 78, akhiri siklus tanpa order.
5. Jika lolos validasi, jalankan:
   python3 /home/hermes/bybit-bot/start.py execute --symbol <SYMBOL> --side <LONG/SHORT> --margin <MARGIN> --leverage <LEV> --tp <TP_PCT> --sl <SL_PCT>
6. Tampilkan ringkasan singkat hasil eksekusi.
EOF
```

## Tahap 5: Tes tiap profile

Tes `analyst`:

```bash
hermes --profile analyst -p "Analisa simbol SOLUSDT: harga 142.5, tren 4H UP, RSI 1H 61, volume spike 1.8x, funding 0.0001."
```

Tes `trader`:

```bash
hermes --profile trader -p "Jalankan satu siklus scan dan evaluasi kandidat Bybit Futures sekarang."
```

## Tahap 6: Tes dulu di demo dan backtest

Sebelum memakai uang asli, jalankan di akun demo Bybit:

```bash
export BYBIT_DEMO=true
python3 ~/bybit-bot/start.py scan
```

Backtest sinyal di data historis (sekitar 41 hari, candle 1H) untuk tiap simbol:

```bash
python3 ~/bybit-bot/start.py backtest --symbol SOLUSDT
python3 ~/bybit-bot/start.py backtest --symbol SOLUSDT --be 0   # tanpa geser SL ke break-even
```

Kalau `total_pnl_pct_margin` negatif di sebagian besar simbol, setting belum layak dipakai dengan uang asli. Data 41 hari menghasilkan sedikit trade, jadi anggap hasilnya sebagai indikasi awal.

## Tahap 7: Jalankan otomatis

Siklus trader lewat cron Hermes:

```bash
hermes cron add --every "15m" --profile trader --prompt "Jalankan siklus Bybit Futures sesuai instruksi di SOUL.md."
hermes cron list
```

Geser SL ke break-even (tanpa AI) lewat crontab sistem:

```bash
crontab -e
```

```
*/15 * * * * set -a; . ~/.hermes/.env; set +a; python3 ~/bybit-bot/start.py manage >> ~/bybit-bot/manage.log 2>&1
```

## Perintah `start.py`

| Perintah | Fungsi |
|---|---|
| `scan` | Cek saldo, posisi aktif, dan cari kandidat sinyal |
| `execute --symbol SOLUSDT --side LONG` | Buka posisi market dengan TP/SL native. Opsi: `--margin`, `--leverage`, `--tp`, `--sl` |
| `manage` | Geser SL ke break-even untuk posisi yang profitnya melewati `--be` (default 7.5) |
| `backtest --symbol SOLUSDT` | Uji strategi di data historis. Opsi: `--tp`, `--sl`, `--leverage`, `--be` |

## Catatan risiko

- Trading futures ber-leverage bisa menghabiskan seluruh margin. Pakai modal yang siap hilang.
- Jarak SL ke harga likuidasi mengecil saat leverage naik. Di 15x likuidasi sekitar 6%, sedangkan SL 5%, jadi pergerakan cepat bisa menembus SL sebelum order tereksekusi.
- Bot ini belum terbukti menguntungkan. Backtest dan demo dulu sebelum live.
