# Biashara AI — Backend

FastAPI service powering Biashara AI: AI reasoning (Claude), machine-learning
insights (scikit-learn), M-Pesa payments (Safaricom Daraja), and Firestore access.

All secrets live server-side — the mobile app never holds an API key.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
```

Then:
1. Copy `.env.example` to `.env` and fill in your credentials
2. Add your Firebase service account key as `firebase-key.json`
   (Firebase Console → Project settings → Service accounts → Generate new private key)

## Running

```bash
# Standard development
uvicorn main:app --reload --port 8000

# With an ngrok tunnel, needed for M-Pesa callbacks
python main.py
```

Interactive API docs: `http://localhost:8000/docs`

> **Note:** ngrok's free tier issues a new URL on every restart. Copy the printed
> URL into `MPESA_CALLBACK_BASE_URL` in `.env` and restart before testing M-Pesa.

## Endpoints

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Health check + config status |
| POST | `/advisor` | AI business advice grounded in the user's real data |
| GET | `/insights/{uid}` | Cashflow forecast, tax estimate, health score |
| POST | `/whatsapp-draft` | AI-drafted customer follow-up message |
| POST | `/mpesa/stk-push` | Trigger an M-Pesa payment prompt |
| POST | `/mpesa-callback` | Receives payment confirmation from Safaricom |

## How the intelligence works

**Cashflow forecast** — `LinearRegression` fitted to the running daily balance,
projected forward 7 days. Returns `has_enough_data: false` below 5 days of
records rather than inventing a number.

**Reorder suggestions** (computed client-side from `stockMovements`) —
`dailyRate = units sold ÷ days elapsed`, then `target = dailyRate × 14 days cover`.
Fully transparent, no black box.

**Health score** — four weighted components scored over a rolling 30-day window:
profitability (0–35), expense control (0–35), income consistency (0–15) and
record keeping (0–15). Solvency deliberately outweighs habit, so a business
burning more than it earns cannot score well simply for keeping tidy books.
Scoring a recent window rather than lifetime totals also keeps it consistent
with the cashflow forecast. An internal indicator built from the user's own
records; explicitly *not* a credit score.

## ⚠️ Tax rates

`TAX_CONFIG` in `main.py` holds Kenyan Turnover Tax and VAT figures. These change
with each annual Finance Act, and public sources currently disagree on both the
rate and the threshold. **Verify against [kra.go.ke](https://www.kra.go.ke)** and
update that block before relying on the output. All tax figures are labelled as
estimates in the UI.

## Security

`.env` and `firebase-key.json` contain secrets and are gitignored — never commit them.
CORS is restricted to local development origins; widen it deliberately when deploying.

## Author

Helen Drar Belay — USIU-Africa, Applied Computer Technology
