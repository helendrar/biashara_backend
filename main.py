"""
Biashara AI — Backend

FastAPI service handling AI reasoning (Claude), M-Pesa payments (Safaricom Daraja),
machine-learning insights (scikit-learn), and Firestore data access.

Run:
    uvicorn main:app --reload --port 8000      # standard development
    python main.py                             # with an ngrok tunnel for M-Pesa callbacks
"""

import asyncio
import base64
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import anthropic
import firebase_admin
import numpy as np
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, firestore
from pydantic import BaseModel
from sklearn.linear_model import LinearRegression

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("biashara")

# ── Startup configuration check ──────────────────────────────
REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "MPESA_CONSUMER_KEY",
    "MPESA_CONSUMER_SECRET",
    "MPESA_SHORTCODE",
    "MPESA_PASSKEY",
]
_missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
if _missing:
    logger.warning("Missing environment variables: %s", ", ".join(_missing))
    logger.warning("Copy .env.example to .env and fill in your values.")

# ── Firebase ─────────────────────────────────────────────────
# Firebase credentials.
#
# Locally we read firebase-key.json. On a hosted platform there is no file to
# upload, so FIREBASE_CREDENTIALS holds the same JSON as an environment
# variable — which also keeps the secret out of the repo.
_firebase_json = os.getenv("FIREBASE_CREDENTIALS")
if _firebase_json:
    cred = credentials.Certificate(json.loads(_firebase_json))
else:
    cred = credentials.Certificate("firebase-key.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Claude ───────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
CLAUDE_MODEL = "claude-sonnet-4-6"

# ── M-Pesa ───────────────────────────────────────────────────
MPESA_CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY")
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET")
MPESA_SHORTCODE = os.getenv("MPESA_SHORTCODE")
MPESA_PASSKEY = os.getenv("MPESA_PASSKEY")
MPESA_CALLBACK_BASE_URL = os.getenv("MPESA_CALLBACK_BASE_URL", "")
CALLBACK_URL = f"{MPESA_CALLBACK_BASE_URL}/mpesa-callback"

# ─────────────────────────────────────────────────────────────
# KENYAN TAX CONFIGURATION
#
# ⚠️  VERIFY THESE AGAINST https://www.kra.go.ke BEFORE RELYING ON THEM.
# Kenyan tax rates change with each annual Finance Act, and public sources
# currently disagree on both the Turnover Tax rate (1% / 1.5% / 3%) and the
# upper threshold (KES 25M vs 50M).
#
# Everything computed from these values is labelled an ESTIMATE in the UI.
# Update `last_verified` whenever you re-check the official figures.
# ─────────────────────────────────────────────────────────────
TAX_CONFIG = {
    "turnover_tax_rate": 0.01,
    "tot_min_annual_turnover": 1_000_000,
    "tot_max_annual_turnover": 25_000_000,
    "vat_rate": 0.16,
    "vat_registration_threshold": 5_000_000,
    "last_verified": "not yet verified — check kra.go.ke",
}

# How far back the health score looks. Scoring recent performance rather than
# lifetime totals keeps it consistent with the cashflow forecast.
HEALTH_WINDOW_DAYS = 30

app = FastAPI(title="Biashara AI Backend", version="1.0.0")

# Local dev origins plus anything listed in ALLOWED_ORIGINS (comma-separated),
# so the deployed web build can be added without a code change.
_default_origins = [
    "http://localhost:8081",
    "http://localhost:19006",
    "http://127.0.0.1:8081",
    "http://localhost:3000",
]
_extra_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_default_origins + _extra_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═════════════════════════════════════════════════════════════
# Health
# ═════════════════════════════════════════════════════════════
@app.get("/")
def health_check():
    return {
        "status": "Biashara AI backend is running",
        "config_ok": not _missing,
        "mpesa_callback_configured": bool(MPESA_CALLBACK_BASE_URL),
    }


# ═════════════════════════════════════════════════════════════
# Shared data access
# ═════════════════════════════════════════════════════════════
def _fetch(collection_name: str, uid: str):
    return [d.to_dict() for d in db.collection(collection_name).where("uid", "==", uid).stream()]


def _sales_velocity(uid: str):
    """
    Units sold per day per item, derived from the stockMovements audit trail.
    Gives the advisor real restock signal rather than just current quantities.
    """
    movements = _fetch("stockMovements", uid)
    by_item = defaultdict(list)
    for m in movements:
        if m.get("quantityChange", 0) < 0 and hasattr(m.get("createdAt"), "date"):
            by_item[m.get("itemName")].append(m)

    velocity = {}
    for name, sales in by_item.items():
        if len(sales) < 2:
            continue
        sales.sort(key=lambda m: m["createdAt"])
        span_days = max(1, (sales[-1]["createdAt"] - sales[0]["createdAt"]).days)
        total = sum(abs(m["quantityChange"]) for m in sales)
        velocity[name] = round(total / span_days, 2)
    return velocity


def _get_profile(uid: str):
    """Shop profile lives at profiles/{uid} — keyed by document id, not a uid field."""
    try:
        snap = db.collection("profiles").document(uid).get()
        return snap.to_dict() if snap.exists else None
    except Exception:
        return None


def get_business_context(uid: str) -> str:
    """Flattens a user's real business data into plain text for Claude to reason over."""
    profile = _get_profile(uid)
    transactions = _fetch("transactions", uid)
    inventory = _fetch("inventory", uid)
    customers = _fetch("customers", uid)
    velocity = _sales_velocity(uid)

    income = sum(_amount(t) for t in transactions if t.get("type") == "income")
    expenses = sum(_amount(t) for t in transactions if t.get("type") == "expense")

    lines = []
    if profile:
        lines.append(f"Shop: {profile.get('shopName', 'Unknown')}")
        lines.append(f"Owner: {profile.get('ownerName', 'Unknown')}")
        lines.append("")

    lines += [
        f"Total income logged: KES {income}",
        f"Total expenses logged: KES {expenses}",
        f"Current balance: KES {income - expenses}",
        "",
        f"Transactions ({len(transactions)} total):",
    ]
    for t in transactions:
        cat = f" [{t.get('category')}]" if t.get("category") else ""
        lines.append(f"- {t.get('type')}: KES {t.get('amount')} — {t.get('note')}{cat}")

    lines.append("")
    lines.append(f"Inventory ({len(inventory)} items):")
    for i in inventory:
        name = i.get("itemName")
        qty = i.get("quantity", 0)
        row = f"- {name}: qty {qty} (low stock threshold {i.get('lowStockThreshold')})"
        if i.get("unitPrice"):
            row += f", sells at KES {i.get('unitPrice')} each"
        rate = velocity.get(name)
        if rate:
            days_left = round(qty / rate, 1) if rate > 0 else None
            row += f" — selling ~{rate}/day"
            if days_left is not None:
                row += f", about {days_left} days of stock left"
        else:
            row += " — not enough sales history to estimate a rate"
        expires = i.get("expiresAt")
        if expires and hasattr(expires, "date"):
            row += f", expires {expires.date().isoformat()}"
        lines.append(row)

    lines.append("")
    lines.append(f"Customers ({len(customers)} purchases logged):")
    for c in customers:
        lines.append(
            f"- {c.get('customerName')}: bought '{c.get('purchaseNote')}' "
            f"for KES {c.get('purchaseAmount')}"
        )

    # Top-selling products, from stock-linked sales (FR-13). Gives the advisor
    # real structured data instead of making it parse free-text purchase notes.
    by_item = defaultdict(lambda: {"units": 0, "sales": 0, "revenue": 0})
    for c in customers:
        item = c.get("itemName")
        if not item:
            continue
        by_item[item]["units"] += c.get("quantity") or 0
        by_item[item]["sales"] += 1
        by_item[item]["revenue"] += c.get("purchaseAmount") or 0

    if by_item:
        ranked = sorted(by_item.items(), key=lambda kv: -kv[1]["units"])[:5]
        lines.append("")
        lines.append("Top-selling products:")
        for name, d in ranked:
            lines.append(
                f"- {name}: {d['units']} units across {d['sales']} sales, "
                f"KES {d['revenue']} revenue"
            )

    # Spending by category, so "where is my money going?" has a real answer.
    by_cat = defaultdict(float)
    for t in transactions:
        if t.get("type") == "expense" and t.get("category"):
            by_cat[t["category"]] += _amount(t)
    if by_cat:
        lines.append("")
        lines.append("Expenses by category:")
        for cat, amt in sorted(by_cat.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {cat}: KES {round(amt)}")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════
# AI Business Advisor
# ═════════════════════════════════════════════════════════════
class AdvisorQuestion(BaseModel):
    uid: str
    question: str


@app.post("/advisor")
def ask_advisor(payload: AdvisorQuestion):
    if not payload.uid or not payload.question.strip():
        raise HTTPException(status_code=400, detail="uid and question are required")

    try:
        context = get_business_context(payload.uid)
    except Exception as e:
        logger.error("Failed to load business context for %s: %s", payload.uid, e)
        raise HTTPException(status_code=503, detail="Couldn't read your business data right now.")

    # FR-16: reply in the owner's chosen language. Their profile language is the
    # default, but if they clearly write in the other language we follow them —
    # a shopkeeper switching mid-conversation shouldn't get answered in the wrong one.
    profile = _get_profile(payload.uid)
    lang = (profile or {}).get("language", "en")
    language_rule = (
        "Reply in clear, natural Kiswahili as spoken in Kenya — the everyday register a "
        "shopkeeper actually uses, not formal textbook Swahili. Keep product names, "
        "'M-Pesa' and 'KES' as they are. If the owner writes to you in English, reply in English instead."
        if lang == "sw"
        else "Reply in English. If the owner writes to you in Kiswahili, reply in Kiswahili instead."
    )

    system_prompt = f"""You are a friendly, practical business advisor for a small business owner in Kenya.
Address them by name if their name appears in the data below.
You have access to their real business data below. Answer their question using ONLY this data.
Be specific — reference actual numbers, items, and customer names from the data when relevant.
Keep answers short and conversational, like you're talking to them in person, not writing a report.
If the data doesn't have enough to answer confidently, say so honestly rather than guessing.

LANGUAGE: {language_rule}

BUSINESS DATA:
{context}
"""

    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": payload.question}],
        )
        return {"answer": response.content[0].text}
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        raise HTTPException(
            status_code=502, detail="The AI advisor is unavailable right now. Please try again."
        )


# ═════════════════════════════════════════════════════════════
# Insights — forecast, tax estimate, health score
# ═════════════════════════════════════════════════════════════
def _amount(t) -> float:
    """
    Safely coerce a transaction amount.

    Firestore can legitimately hold null or a string here (a partially-failed
    write, or data seeded by an older schema), and an unguarded arithmetic op
    on None takes down the whole endpoint. Anything non-numeric counts as zero.
    """
    v = t.get("amount")
    if isinstance(v, bool) or v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _daily_net_series(transactions):
    """Collapse transactions into {date: net_amount} for time-series work."""
    daily = defaultdict(float)
    for t in transactions:
        created = t.get("createdAt")
        if not created or not hasattr(created, "date"):
            continue
        amount = _amount(t)
        daily[created.date()] += amount if t.get("type") == "income" else -amount
    return dict(sorted(daily.items()))


def forecast_cashflow(transactions, days_ahead: int = 7):
    """
    Linear-regression cashflow forecast over the running daily balance.

    Deliberately simple and explainable: fits a straight line to the cumulative
    balance over time and projects it forward. Returns has_enough_data=False
    rather than inventing numbers for users with little history.
    """
    daily = _daily_net_series(transactions)
    if len(daily) < 5:
        return {"has_enough_data": False, "reason": "Need at least 5 days of records."}

    dates = list(daily.keys())
    start = dates[0]

    running, cumulative = [], 0.0
    for d in dates:
        cumulative += daily[d]
        running.append(cumulative)

    X = np.array([[(d - start).days] for d in dates])
    y = np.array(running)

    model = LinearRegression().fit(X, y)
    r_squared = float(model.score(X, y))
    slope = float(model.coef_[0])

    last_day = int(X[-1][0])
    future_X = np.array([[last_day + i] for i in range(1, days_ahead + 1)])
    predictions = model.predict(future_X)

    current_balance = float(running[-1])
    days_until_zero = None
    if slope < 0 and current_balance > 0:
        days_until_zero = int(current_balance / abs(slope))

    return {
        "has_enough_data": True,
        "current_balance": round(current_balance, 2),
        "projected_balance": round(float(predictions[-1]), 2),
        "daily_trend": round(slope, 2),
        "days_ahead": days_ahead,
        "confidence": round(max(0.0, r_squared), 2),
        "days_until_zero": days_until_zero,
        "data_points": len(daily),
        "projection": [
            {"day": i + 1, "balance": round(float(p), 2)} for i, p in enumerate(predictions)
        ],
    }


def estimate_tax(transactions):
    """
    Estimates Kenyan Turnover Tax from logged income only.
    An ESTIMATE from the user's own records — not tax advice, and not a
    substitute for filing through iTax.
    """
    now = datetime.now(timezone.utc)
    year_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)

    ytd_income = 0.0
    days_recorded = set()
    for t in transactions:
        created = t.get("createdAt")
        if not created or t.get("type") != "income":
            continue
        created_utc = created if getattr(created, "tzinfo", None) else created.replace(tzinfo=timezone.utc)
        if created_utc >= year_start:
            ytd_income += _amount(t)
            days_recorded.add(created_utc.date())

    days_elapsed = max(1, (now - year_start).days)
    annualised = (ytd_income / days_elapsed) * 365

    rate = TAX_CONFIG["turnover_tax_rate"]
    eligible = (
        TAX_CONFIG["tot_min_annual_turnover"] <= annualised <= TAX_CONFIG["tot_max_annual_turnover"]
    )

    return {
        "ytd_income": round(ytd_income, 2),
        "annualised_projection": round(annualised, 2),
        "rate_used": rate,
        "rate_label": f"{rate * 100:g}% Turnover Tax (estimate)",
        "estimated_annual_tax": round(annualised * rate, 2) if eligible else 0,
        "estimated_monthly_tax": round((annualised * rate) / 12, 2) if eligible else 0,
        "tot_eligible": eligible,
        "below_threshold": annualised < TAX_CONFIG["tot_min_annual_turnover"],
        "above_threshold": annualised > TAX_CONFIG["tot_max_annual_turnover"],
        "vat_threshold_crossed": annualised > TAX_CONFIG["vat_registration_threshold"],
        "days_with_records": len(days_recorded),
        "disclaimer": (
            "Estimate based only on income you logged here. Verify current rates at "
            "kra.go.ke and consult a tax professional before filing."
        ),
    }


def financial_health_score(transactions):
    """
    A transparent 0–100 internal health indicator. NOT a credit score — it uses
    no external credit data, and every point traces back to the user's own records.

    Weighting is deliberately lopsided towards solvency:

        Profitability     0–35   is money actually left over?
        Expense control   0–35   what share of income goes straight back out?
        Income consistency 0–15  how many distinct days brought money in?
        Record keeping    0–15   are they logging regularly?

    Habit (the last two) is worth real points but must never outweigh solvency —
    otherwise a business burning 3× its income would score "Fair" simply for
    keeping tidy books, which would be actively misleading.
    """
    all_valid = [t for t in transactions if hasattr(t.get("createdAt"), "date") and t.get("amount")]
    if len(all_valid) < 3:
        return {"has_enough_data": False, "reason": "Log a few more transactions first."}

    # Score RECENT performance, not lifetime totals. A shop that was profitable
    # months ago but is bleeding now is not healthy — and scoring it "Strong"
    # would contradict the cashflow forecast sitting right next to it.
    cutoff = datetime.now(timezone.utc) - timedelta(days=HEALTH_WINDOW_DAYS)
    recent = [
        t for t in all_valid
        if (t["createdAt"] if t["createdAt"].tzinfo else t["createdAt"].replace(tzinfo=timezone.utc)) >= cutoff
    ]
    # Fall back to everything if the recent window is too sparse to judge.
    valid = recent if len(recent) >= 3 else all_valid
    windowed = valid is recent

    income = [t for t in valid if t.get("type") == "income"]
    expenses = [t for t in valid if t.get("type") == "expense"]
    total_income = sum(_amount(t) for t in income)
    total_expenses = sum(_amount(t) for t in expenses)

    # 1. Profitability (0–35) — full marks at a 35% margin or better.
    if total_income == 0:
        profitability = 0
    else:
        margin = (total_income - total_expenses) / total_income
        profitability = max(0, min(35, int(margin * 100)))

    # 2. Expense control (0–35)
    if total_income == 0:
        expense_control = 0
    else:
        ratio = total_expenses / total_income
        expense_control = 35 if ratio <= 0.6 else 21 if ratio <= 0.8 else 10 if ratio <= 1.0 else 0

    # 3. Income consistency (0–15) — distinct days money came in.
    income_days = {t["createdAt"].date() for t in income}
    consistency = int(min(15, len(income_days) * 1.5))

    # 4. Record keeping (0–15) — share of days in the period with any entry.
    all_days = {t["createdAt"].date() for t in valid}
    span = max(1, (max(all_days) - min(all_days)).days + 1)
    record_keeping = int(min(15, (len(all_days) / span) * 15))

    total = profitability + expense_control + consistency + record_keeping
    band = (
        "Strong" if total >= 75
        else "Fair" if total >= 50
        else "Needs attention" if total >= 25
        else "At risk"
    )

    return {
        "has_enough_data": True,
        "score": total,
        "band": band,
        "loss_making": total_expenses > total_income,
        "window_days": HEALTH_WINDOW_DAYS if windowed else None,
        "breakdown": [
            {"label": "Profitability", "score": profitability, "max": 35},
            {"label": "Expense control", "score": expense_control, "max": 35},
            {"label": "Income consistency", "score": consistency, "max": 15},
            {"label": "Record keeping", "score": record_keeping, "max": 15},
        ],
        "disclaimer": (
            "An internal indicator built from your own records only — not a credit "
            "score, and not used by any lender."
        ),
    }


@app.get("/insights/{uid}")
def get_insights(uid: str):
    try:
        transactions = _fetch("transactions", uid)
    except Exception as e:
        logger.error("Insights fetch failed for %s: %s", uid, e)
        raise HTTPException(status_code=503, detail="Couldn't read your business data right now.")

    return {
        "forecast": forecast_cashflow(transactions),
        "tax": estimate_tax(transactions),
        "health": financial_health_score(transactions),
    }


# ═════════════════════════════════════════════════════════════
# WhatsApp follow-up drafts
# ═════════════════════════════════════════════════════════════
class WhatsAppDraftRequest(BaseModel):
    uid: str
    customer_name: str


@app.post("/whatsapp-draft")
def whatsapp_draft(payload: WhatsAppDraftRequest):
    if not payload.uid or not payload.customer_name.strip():
        raise HTTPException(status_code=400, detail="uid and customer_name are required")

    try:
        all_purchases = _fetch("customers", payload.uid)
    except Exception as e:
        logger.error("WhatsApp draft fetch failed: %s", e)
        raise HTTPException(status_code=503, detail="Couldn't read customer data.")

    target = payload.customer_name.strip().lower()
    history = [c for c in all_purchases if (c.get("customerName") or "").lower() == target]
    if not history:
        raise HTTPException(status_code=404, detail="No purchase history for that customer.")

    history.sort(
        key=lambda c: c.get("lastPurchaseAt") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    total_spent = sum(c.get("purchaseAmount") or 0 for c in history)
    last = history[0]
    last_date = last.get("lastPurchaseAt")
    days_since = (datetime.now(timezone.utc) - last_date).days if last_date else None

    recent = "\n".join(
        f"- {c.get('purchaseNote') or 'purchase'}: KES {c.get('purchaseAmount')}" for c in history[:5]
    )

    profile = _get_profile(payload.uid)
    draft_lang = (profile or {}).get("language", "en")
    draft_language_rule = (
        "Write the message in natural, everyday Kenyan Kiswahili — the way a shopkeeper "
        "actually texts a regular customer. Keep product names as they are."
        if draft_lang == "sw"
        else "Write the message in English."
    )

    system_prompt = f"""You write short, warm WhatsApp follow-up messages for a small shop owner in Kenya.

LANGUAGE: {draft_language_rule}

Customer: {payload.customer_name}
Times purchased: {len(history)}
Total spent: KES {total_spent}
Most recent: {last.get('purchaseNote') or 'a purchase'} (KES {last.get('purchaseAmount')})
Days since last purchase: {days_since if days_since is not None else 'unknown'}

Recent purchases:
{recent}

Rules:
- Keep it under 40 words. This is WhatsApp, not email.
- Friendly and natural — the way a real shopkeeper talks to a regular customer.
- Reference what they actually bought. Never invent products, prices, or offers.
- No emoji spam; one at most.
- Do not promise discounts or deals — the owner hasn't authorised any.
- Output ONLY the message text, nothing else."""

    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            system=system_prompt,
            messages=[{"role": "user", "content": "Write the follow-up message."}],
        )
        draft = response.content[0].text.strip()
    except Exception as e:
        logger.error("Claude WhatsApp draft failed: %s", e)
        raise HTTPException(status_code=502, detail="Couldn't generate a message right now.")

    raw_phone = (last.get("phone") or "").replace(" ", "").replace("+", "")
    if raw_phone.startswith("0"):
        raw_phone = "254" + raw_phone[1:]

    return {
        "draft": draft,
        "phone": raw_phone,
        "whatsapp_url": f"https://wa.me/{raw_phone}?text={quote(draft)}" if raw_phone else None,
        "days_since_last_purchase": days_since,
        "total_spent": total_spent,
        "purchase_count": len(history),
    }


# ═════════════════════════════════════════════════════════════
# M-Pesa (Safaricom Daraja — sandbox)
# ═════════════════════════════════════════════════════════════
def get_mpesa_token() -> str:
    url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    try:
        response = requests.get(url, auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET), timeout=15)
        response.raise_for_status()
        return response.json()["access_token"]
    except Exception as e:
        logger.error("M-Pesa auth failed: %s", e)
        raise HTTPException(status_code=502, detail="Couldn't authenticate with M-Pesa.")


class STKPushRequest(BaseModel):
    uid: str
    phone: str
    amount: int
    note: str


@app.post("/mpesa/stk-push")
def stk_push(payload: STKPushRequest):
    if not MPESA_CALLBACK_BASE_URL:
        raise HTTPException(
            status_code=503,
            detail="M-Pesa callback URL isn't configured. Set MPESA_CALLBACK_BASE_URL in .env.",
        )
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")

    token = get_mpesa_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(
        (MPESA_SHORTCODE + MPESA_PASSKEY + timestamp).encode()
    ).decode()

    phone = payload.phone.strip().replace(" ", "")
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    elif phone.startswith("+"):
        phone = phone[1:]

    body = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": payload.amount,
        "PartyA": phone,
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": CALLBACK_URL,
        "AccountReference": "BiasharaAI",
        "TransactionDesc": payload.note[:20] or "Payment",
    }

    try:
        response = requests.post(
            "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        result = response.json()
    except Exception as e:
        logger.error("STK push request failed: %s", e)
        raise HTTPException(status_code=502, detail="Couldn't reach M-Pesa. Please try again.")

    if result.get("ResponseCode") == "0":
        db.collection("pendingPayments").add(
            {
                "uid": payload.uid,
                "amount": payload.amount,
                "note": payload.note,
                "checkoutRequestId": result.get("CheckoutRequestID"),
                "status": "pending",
                "createdAt": firestore.SERVER_TIMESTAMP,
            }
        )
        logger.info("STK push accepted: %s", result.get("CheckoutRequestID"))
    else:
        logger.warning("STK push rejected: %s", result)

    return result


def _find_pending_payment(checkout_id: str):
    query = (
        db.collection("pendingPayments")
        .where("checkoutRequestId", "==", checkout_id)
        .limit(1)
        .stream()
    )
    return next(query, None)


@app.post("/mpesa-callback")
async def mpesa_callback(request: dict):
    """
    Receives payment confirmation from Safaricom. On success, writes the
    payment straight into the user's transactions as income.

    Always returns 200 — Safaricom retries on any non-200 response.

    NOTE ON THE RETRY LOOP BELOW: Safaricom's sandbox can call this endpoint
    faster than our own /mpesa/stk-push handler finishes writing the matching
    pendingPayments record to Firestore — the callback and our own database
    write are racing each other, and the callback sometimes wins. Rather than
    silently dropping a real payment because of that timing gap, we retry the
    lookup briefly before giving up.
    """
    logger.info("M-Pesa callback received: %s", request)
    try:
        callback_data = request["Body"]["stkCallback"]
        checkout_id = callback_data["CheckoutRequestID"]
        result_code = callback_data["ResultCode"]
        result_desc = callback_data.get("ResultDesc", "")
        logger.info("Callback %s → code %s (%s)", checkout_id, result_code, result_desc)

        pending_doc = _find_pending_payment(checkout_id)

        if not pending_doc:
            logger.info("No pending payment yet for %s — retrying briefly", checkout_id)
            for attempt in range(4):
                await asyncio.sleep(1)
                pending_doc = _find_pending_payment(checkout_id)
                if pending_doc:
                    logger.info("Found pending payment for %s on retry %d", checkout_id, attempt + 1)
                    break

        if not pending_doc:
            logger.warning("No matching pending payment for %s after retries", checkout_id)
            return {"ResultCode": 0, "ResultDesc": "Accepted"}

        data = pending_doc.to_dict()
        if result_code == 0:
            db.collection("transactions").add(
                {
                    "amount": data["amount"],
                    "note": data["note"],
                    "type": "income",
                    "category": "Sales",
                    "uid": data["uid"],
                    "source": "mpesa",
                    "createdAt": firestore.SERVER_TIMESTAMP,
                }
            )
            pending_doc.reference.update({"status": "completed", "resultDesc": result_desc})
            logger.info("Payment completed and logged as income.")
        else:
            pending_doc.reference.update({"status": "failed", "resultDesc": result_desc})
            logger.info("Payment marked failed: %s", result_desc)

    except Exception as e:
        logger.error("Callback handling error: %s", e)

    return {"ResultCode": 0, "ResultDesc": "Accepted"}


# ═════════════════════════════════════════════════════════════
# Local dev entrypoint (with ngrok tunnel for M-Pesa callbacks)
# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    from pyngrok import conf, ngrok

    token = os.getenv("NGROK_AUTHTOKEN")
    if token:
        conf.get_default().auth_token = token

    tunnel = ngrok.connect(8000)
    public_url = tunnel.public_url
    print(f"\n🔗 Public callback URL: {public_url}/mpesa-callback")
    print("   Copy this into MPESA_CALLBACK_BASE_URL in your .env, then restart.\n")

    uvicorn.run(app, host="0.0.0.0", port=8000)
