# Biashara AI — Code Walkthrough

A map of the system: what each part does, where it lives, and why it was built
that way. Use this to find things quickly and to answer "show me where X happens."

---

## Architecture in one paragraph

Three layers. A **React Native (Expo) app** runs on phone and web. A **FastAPI
backend** handles everything requiring a secret or heavy computation. **Firebase**
provides authentication and a Firestore database. The app never calls Claude or
Safaricom directly — those keys live only on the server, which is both a security
requirement (NFR-04) and the reason the architecture has three tiers rather than two.

```
┌─────────────────────────────────────────┐
│  React Native app (Expo Router)         │
│  Finance · Stock · Sales · Insights ·   │
│  Advisor                                │
└──────────┬──────────────────┬───────────┘
           │                  │
   Firebase SDK          HTTPS (fetch)
           │                  │
┌──────────▼────────┐  ┌──────▼────────────────────┐
│ Firebase          │  │ FastAPI backend           │
│ • Auth            │◄─┤ • Claude API (advisor,    │
│ • Firestore       │  │   WhatsApp drafts)        │
│ • Security rules  │  │ • scikit-learn (forecast) │
└───────────────────┘  │ • M-Pesa Daraja           │
                       └───────────────────────────┘
```

**Why the app talks to Firestore directly but routes AI through the backend:**
Firestore enforces per-user access at the database level via security rules, so
direct access is safe. Claude and M-Pesa require secret keys, which cannot be
shipped inside a mobile app — anyone can decompile it.

---

## Frontend

### `src/app/` — screens (file-based routing)

| File | Requirement | What it does |
|---|---|---|
| `index.tsx` | FR-01, 03, 05 | Auth (login/signup/reset) **and** the Finance dashboard. Balance, cashflow charts, transaction ledger with edit/delete, M-Pesa collection. |
| `inventory.tsx` | FR-08, 09, 10 | Stock with a tap-to-add catalogue (~80 duka products), low-stock and expiry alerts, velocity-based reorder suggestions. |
| `sales.tsx` | FR-11, 12, 13, 14 | Customer purchases. "From your stock" mode decrements real inventory; top-selling products; AI WhatsApp follow-ups. |
| `insights.tsx` | FR-06, 07 | Cashflow forecast, business health score, tax estimate. |
| `advisor.tsx` | FR-15, 16 | Chat with Claude over the owner's real data. |
| `_layout.tsx` | — | Wraps everything in `ErrorBoundary` and `LanguageProvider`. |

### `src/lib/` — logic, deliberately separated from UI

| File | Purpose |
|---|---|
| `analytics.ts` | FR-05 aggregation: `summarise`, `bucketise`, `expenseBreakdown`, and `num()` — the numeric coercion guard. |
| `i18n.ts` | FR-16. 186 bilingual strings. Display-only; stored values stay English. |
| `LanguageContext.tsx` | Language state, persisted to the Firestore profile. |
| `errors.ts` | Translates Firebase error codes into human sentences; shared form validators. |
| `dates.ts` | Timestamp formatting, day grouping, `netTotal`. |
| `profile.ts` | FR-02 shop profile at `profiles/{uid}`. |
| `categories.ts` | FR-03 transaction categories. |

### `src/components/ui/` — the design system

`Ledger.tsx` holds every shared primitive: `Screen`, `ScreenHeader`, `StatBlock`,
`LedgerRow`, `LedgerInput`, `LedgerButton`, `LedgerToggle`. Changing a button
everywhere means changing one file.

`CashflowChart.tsx` draws the FR-05 charts using plain Views — no charting
library. Smaller bundle, identical rendering on web and native, and it matches
the design language exactly.

`Feedback.tsx`, `ConfirmDialog.tsx`, `LanguageToggle.tsx` cover error/success
banners, skeletons, and the cross-platform confirm dialog (a Modal rather than
`Alert.alert`, which is unreliable on React Native Web).

---

## Backend — `main.py`

| Endpoint | Requirement | What it does |
|---|---|---|
| `GET /` | — | Health check; reports whether config is complete. |
| `POST /advisor` | FR-15, 16 | Builds a text summary of the owner's real data, sends it to Claude with a language instruction. |
| `GET /insights/{uid}` | FR-06, 07 | Returns forecast + health score + tax estimate. |
| `POST /whatsapp-draft` | FR-14 | Claude writes a follow-up from real purchase history; returns a `wa.me` deep link. |
| `POST /mpesa/stk-push` | FR-04 | Triggers a payment prompt on the customer's phone. |
| `POST /mpesa-callback` | FR-04 | Receives Safaricom's confirmation; writes the sale as income. |

### The three analysis functions

**`forecast_cashflow`** — fits `LinearRegression` to the running daily balance and
projects 7 days forward. Returns `has_enough_data: false` below 5 days rather than
inventing a number. Also computes days-until-zero when the trend is negative.

**`financial_health_score`** — 0–100 from four weighted components over a rolling
30-day window: profitability (35), expense control (35), income consistency (15),
record keeping (15).

**`estimate_tax`** — annualises year-to-date income, applies the Turnover Tax rate
from `TAX_CONFIG`, flags both the eligibility floor and the VAT threshold.

### `seed_data.py`

Generates ~45 days of realistic backdated activity. Necessary because
`serverTimestamp()` stamps everything "now" — clicking demo data by hand leaves
Insights and reorder suggestions empty, since both read date spans.

---

## Data model

| Collection | Written by | Key fields |
|---|---|---|
| `profiles/{uid}` | app | shopName, ownerName, category, language |
| `transactions` | app + backend | amount, type, category, note, createdAt, source |
| `inventory` | app | itemName, quantity, lowStockThreshold, unitPrice, supplierNote, expiresAt |
| `stockMovements` | app | itemId, quantityChange, reason, createdAt |
| `customers` | app | customerName, phone, purchaseNote, purchaseAmount, itemName, quantity |
| `advisorMessages` | app | role, text, createdAt |
| `pendingPayments` | backend only | checkoutRequestId, amount, status |

**Why `stockMovements` exists separately from `inventory`:** inventory stores a
single current quantity, overwritten on every change. Without an event log there
is no history — and no history means no sales velocity, which means no reorder
suggestion. Every stock change writes a timestamped record, which doubles as an
audit trail.

---

## Three design decisions worth defending

**1. Stored values stay English; only display is translated.**
Transaction categories and health bands are written to Firestore in English and
translated at render time. Translating stored data would break existing records
and make the backend's analysis language-dependent.

**2. Reorder suggestions use transparent arithmetic, not a trained model.**
`dailyRate = units sold ÷ days elapsed`, then `target = dailyRate × 14 days`. Every
number is explainable to the shopkeeper and defensible in a viva. A neural network
on a few weeks of single-shop data would be less accurate *and* unexplainable.

**3. Client-side sorting instead of Firestore `orderBy`.**
Compound queries (`where` + `orderBy`) require a composite index in Firestore; a
missing one fails silently and leaves the screen blank. Sorting in memory removes
that fragility — at these data volumes the cost is negligible.

---

## Two bugs found by testing, worth mentioning as process evidence

**Null amounts crashed `/insights`.** A transaction with `amount: null` — which
Firestore can legitimately hold — took down the whole endpoint. Fixed with a
coercion helper applied across every aggregation.

**The frontend silently concatenated money.** `?? 0` catches null but not strings,
so `sum + "500"` produced `"0500300"` instead of `800` — a wrong balance shown
confidently, with no error. Fixed with `num()` on every amount path.

Both were caught by executing the logic against deliberately dirty data, not by
reading the code. That is a legitimate point about engineering process, not just
a bug list.
