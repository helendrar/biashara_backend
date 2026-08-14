# Biashara AI — Requirements Status & Viva Defence Guide

---

# PART 1 — Requirements scorecard

## Functional Requirements: 14 of 16 fully delivered

| # | Requirement | Status | Where |
|---|---|---|---|
| FR-01 | Auth, sessions, password reset | ✅ | `index.tsx` |
| FR-02 | Profile: name, category, language | ✅ | `profile.ts` |
| FR-03 | Income/expense with categories | ✅ | `index.tsx`, `categories.ts` |
| FR-04 | M-Pesa integration | ⚠️ Reinterpreted | `main.py` |
| FR-05 | Week/month dashboard + charts | ✅ | `analytics.ts`, `CashflowChart.tsx` |
| FR-06 | ML cashflow forecast | ✅ | `forecast_cashflow()` |
| FR-07 | Tax calculation | ⚠️ Pull, not push | `estimate_tax()` |
| FR-08 | Inventory + price, supplier, expiry | ✅ | `inventory.tsx` |
| FR-09 | Low stock alerts | ✅ | `inventory.tsx` |
| FR-10 | Reorder suggestions | ✅ | `getReorderSuggestion()` |
| FR-11 | Sales decrement stock | ✅ | `sales.tsx` |
| FR-12 | Customer database | ✅ | `sales.tsx` |
| FR-13 | Top-selling products | ✅ | `sales.tsx` |
| FR-14 | AI WhatsApp drafts | ✅ | `/whatsapp-draft` |
| FR-15 | Conversational advisor | ✅ | `/advisor` |
| FR-16 | Bilingual EN/SW | ✅ | `i18n.ts` (186 keys) |

## Non-Functional: 6 of 10 met, 1 partial, 3 outstanding

| # | Requirement | Status |
|---|---|---|
| NFR-01 | SUS ≥ 70 | ❌ Requires fieldwork |
| NFR-02 | Advisor <8s, dashboard <3s | ⚠️ Not formally measured |
| NFR-03 | Render deployment | ✅ once deployed |
| NFR-04 | Security rules | ✅ 6 collections |
| NFR-05 | Scalability | ✅ architectural |
| NFR-06 | Offline sync queue | ❌ Documented as future work |
| NFR-07 | 16sp text, 44dp targets | ✅ |
| NFR-08 | AI disclaimer | ✅ |
| NFR-09 | Android API 26+ | ✅ minSdk 26 |
| NFR-10 | Zero cost | ✅ |

---

# PART 2 — Likely questions, and honest answers

## On the AI

**"Is this actually AI, or just a chatbot wrapper?"**
Three distinct pieces. Claude handles natural-language reasoning over the owner's
real data. A scikit-learn `LinearRegression` produces the cashflow forecast.
Velocity arithmetic drives reorder suggestions. Only the first is an LLM — and
none of it is decorative: each answers a question the owner actually asks.

**"How do you stop it hallucinating numbers?"**
Claude only ever sees data pulled from that user's Firestore documents, assembled
server-side, and the prompt instructs it to answer from that data only and to say
so when the data is insufficient. It has no browsing and no memory between calls.
Every response also carries a visible disclaimer (NFR-08).

**"Why linear regression and not something more sophisticated?"**
Data volume and explainability. A single shop produces weeks of daily records —
far too little to train anything deeper without overfitting. Linear regression is
honest at this scale and its output is explainable to the shopkeeper: "you're
trending down about KES 600 a day." An unexplainable prediction about someone's
livelihood is worse than a simple one.

## On M-Pesa (expect this one)

**"Your proposal said import M-Pesa records. This doesn't do that."**
Correct, and deliberately so. Investigating Daraja showed it exposes no
third-party endpoint for reading a merchant's transaction history — that access
is restricted for privacy and regulatory reasons. What is permitted is initiating
and confirming new payments, so the system captures payments going forward and
writes them straight into the books. That serves the underlying need — "don't
retype what M-Pesa already knows" — for everything from installation onward.
Documented in `REQUIREMENTS_NOTES.md`.

**"Can you demo a successful payment?"**
The full chain is verified in server logs: request accepted, callback received,
matched, written to Firestore. Completing a *successful* simulated payment in
Safaricom's shared sandbox proved unreliable — `ResultCode 1037`, "no response
from user" — which is a widely reported limitation of the shared test shortcode.
The integration is correct; the sandbox is the constraint.

**"How do you know it's the sandbox and not your code?"**
Two ways. The callback arrives and is parsed correctly — Safaricom is reaching my
endpoint and my handler reads it. And the failure code is specifically "no
response from user," meaning the simulated customer never confirmed, which is
upstream of anything my code controls. I also found and fixed a real race
condition in my own handler along the way: Safaricom's callback can arrive before
my own pendingPayments write completes, so the lookup now retries briefly.

## On the tax feature

**"Are these tax figures correct?"**
They're labelled estimates, and that's deliberate. Kenyan rates change with each
Finance Act, and public sources currently disagree on both the Turnover Tax rate
and the threshold. Rather than present an unverifiable number as authoritative
tax advice, all rates sit in a single `TAX_CONFIG` block, the rate used is
displayed on screen, and the UI directs users to verify at kra.go.ke.

**"Why isn't it a push notification like you promised?"**
Push requires either a persistent scheduler or Firebase Cloud Messaging on a paid
plan, which conflicts with the zero-cost constraint (NFR-10). The calculation —
the substantive part — is fully implemented and always available on the Insights
screen. The delivery mechanism was the compromise.

## On architecture

**"Why a separate backend? Firebase could do this."**
API key security. Claude and M-Pesa keys cannot ship inside a mobile app — anyone
can decompile it. The backend is also where scikit-learn runs, which has no
mobile equivalent. Firestore is still accessed directly by the app, because
security rules enforce per-user access at the database level.

**"How is one user's data isolated from another's?"**
Every document carries a `uid`, and Firestore security rules permit read/write
only when `request.auth.uid` matches it. That's enforced server-side by Firebase,
not by app code — a modified client still cannot read another user's data.

**"What happens with no internet?"**
Firestore's SDK caches reads, so previously loaded data still displays. An
explicit write queue with sync-on-reconnect was not built — declared as future
work in `REQUIREMENTS_NOTES.md` rather than claimed.

## On testing and process

**"How do you know it works?"**
TypeScript strict mode compiles clean. The backend analysis functions were
executed against mock data covering empty accounts, malformed records, and
boundary conditions. The M-Pesa callback was tested for all three paths: success
writes a transaction, failure doesn't, malformed input still returns HTTP 200 so
Safaricom stops retrying.

**"Did testing find anything?"**
Two real bugs, both silent. A null amount crashed the entire insights endpoint.
And on the frontend, `?? 0` guarded against null but not strings — so a string
amount concatenated instead of adding, producing a wrong balance displayed with
no error at all. Both were caught by running the code against deliberately dirty
data rather than by reading it.

**"Have you tested with real users?"**
No — and I'm not going to claim otherwise. NFR-01 specifies a System Usability
Scale study with SME owners. That requires fieldwork I haven't completed, so it's
declared as an outstanding limitation.

## On scope

**"Your proposal promised more than this."**
Fourteen of sixteen functional requirements are fully delivered. Two were
reinterpreted after investigating constraints that made the literal wording
impossible — no Daraja history endpoint, no KRA API. Both are documented with the
reasoning. Three non-functional requirements remain open and are declared rather
than hidden. I'd rather present four features that work honestly than sixteen
that half-work.

---

# PART 3 — Demo script (7 minutes)

**Before you start:** open the Render URL in a browser to wake the service — the
free tier sleeps after 15 minutes and takes ~40 seconds to wake.

| Time | Screen | Say |
|---|---|---|
| 0:00 | Login | "Firebase Auth. Each shop's data is isolated by security rules." |
| 0:30 | Finance | "Live balance. This week/month toggle — income vs expenses, and where the money actually goes." |
| 1:30 | Finance | Add a transaction. "Categorised, appears instantly, groups by day." |
| 2:15 | Stock | "Tap-to-add catalogue — 80 common items, because typing product names is why shopkeepers abandon these apps." |
| 3:00 | Stock | Point at reorder suggestion. "Computed from real sales velocity: selling 1.8 a day, 6 days left, buy 20 more." |
| 3:45 | Sales | Log a stock-linked sale. "This decrements real inventory — go back and the quantity changed." |
| 4:30 | Sales | Tap Follow up. "Claude writes a WhatsApp message from actual purchase history." |
| 5:15 | Insights | "scikit-learn forecast, health score, tax estimate — all from their own records." |
| 6:00 | Advisor | Ask "Where is my money going?" then switch to SW and ask again. |
| 6:45 | Close | "14 of 16 requirements delivered. Two reinterpreted after investigating API limits — documented in the repo." |

**Fallback:** have screenshots ready. If the network fails, narrate from those —
far better than silence while something loads.
