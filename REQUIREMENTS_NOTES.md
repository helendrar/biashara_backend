# Requirements — Implementation Notes & Scope Decisions

This document records where the delivered system departs from the original
project proposal, and why. Every deviation below was a deliberate engineering
decision made after investigating the constraint — not an omission.

---

## FR-04 — "Import M-Pesa transaction records"

**Proposal wording:** the system shall import M-Pesa transaction records via the
Daraja API.

**What was found:** Safaricom's Daraja API does not expose a third-party
endpoint for reading a business's historical M-Pesa statement. The available
APIs are transactional (initiating and confirming payments), not retrospective.
Access to a merchant's full transaction history is restricted for privacy and
regulatory reasons, and is not available to sandbox or standard production
integrations.

**What was built instead:** M-Pesa Express (STK Push). When an owner makes a
sale, the app triggers a genuine payment prompt on the customer's phone. On
confirmation, Safaricom calls back to `/mpesa-callback` and the payment is
written directly into the owner's transactions as income — no manual entry.

**Why this is the right interpretation:** the underlying user need is
"payments received through M-Pesa should appear in my books without retyping
them." STK Push satisfies that need going forward, which is the part a third
party is actually permitted to automate. Backfilling history remains manual.

**Verification status:** the full chain — request accepted, callback received,
matched to the pending payment, written to Firestore — has been confirmed in
backend server logs. Completing a *successful* simulated payment in Safaricom's
shared sandbox proved unreliable (`ResultCode 1037`, "no response from user"),
a widely reported limitation of the shared sandbox shortcode rather than a
defect in this integration.

**Known implementation detail:** Safaricom's callback can arrive faster than the
app's own `pendingPayments` write completes. The callback handler retries the
lookup for a few seconds before giving up, so a genuine payment is not lost to
that race.

---

## FR-07 — "KRA tax reminder and calculation"

**Proposal wording:** the system shall send tax reminders and calculate
obligations using KRA data.

**What was found:** KRA provides no public API for third-party tax calculation
or filing. iTax is a closed system. No integration is available to a student
project, or to a commercial one without formal partnership.

**What was built instead:** a Turnover Tax estimator on the Insights screen,
computed from the owner's own logged income. It annualises year-to-date income,
applies the published Turnover Tax rate, and flags both the lower eligibility
threshold and the VAT registration threshold.

**Delivery model — pull, not push:** the proposal implies a proactive
notification. This is delivered as an always-available screen rather than a
scheduled push. Push notifications require either a persistent backend scheduler
or Firebase Cloud Messaging with a paid plan, both outside NFR-10's zero-cost
constraint. The calculation itself — the substantive part of the requirement —
is fully implemented.

**Rate accuracy caveat:** Kenyan tax rates change with each annual Finance Act,
and public sources currently disagree on both the Turnover Tax rate (1% / 1.5% /
3%) and the upper threshold (KES 25M vs 50M). All rates therefore live in a
single `TAX_CONFIG` block in `main.py` and are **labelled as estimates in the
UI**, with a disclaimer directing the user to verify at kra.go.ke. This is a
deliberate honesty measure: presenting an unverifiable figure as authoritative
tax advice would be worse than presenting it as an estimate.

---

## NFR-01 — Usability testing (SUS ≥ 70)

**Status: not yet conducted.** This requires testing with real SME owners and
cannot be simulated or inferred from the codebase. The instrument and protocol
are prepared; the study itself remains outstanding and is declared as a
limitation rather than reported as complete.

---

## NFR-06 — Offline resilience

**Status: not implemented.** Firestore's client SDK provides some offline read
caching by default, but an explicit write queue with sync-on-reconnect was not
built. Declared as future work rather than claimed.

---

## Summary of scope decisions

| Requirement | Proposal | Delivered | Reason |
|---|---|---|---|
| FR-04 | Import M-Pesa history | Capture new payments via STK Push | API access not available to third parties |
| FR-07 | Push tax reminders | On-demand tax estimator screen | No KRA API; push needs paid infrastructure |
| NFR-01 | SUS ≥ 70 | Not yet tested | Requires fieldwork with real users |
| NFR-06 | Offline sync queue | Not implemented | Deferred; declared as future work |
