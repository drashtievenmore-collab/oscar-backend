# Evenmore ERP — Backend

Django + DRF + PostgreSQL implementation of [api.md](api.md) (HTTP contract),
[db.md](db.md) (schema) and [api-integration.md](api-integration.md) (what the
React app expects of the server).

The React app in `Evenmore-ERP/app` now talks to this server. Its side of the
wiring is `src/services/backendSync.js` (the field mapping, one entry per
entity) plus the `persist*` helpers in `src/context/ERPContext.jsx`, which push
every create/update/delete and replace the optimistic record with the server's
copy. The contract is still api.md — the app conforms to it, not the reverse.

Run both: `python manage.py runserver` here, `npm run dev` in `Evenmore-ERP/app`.
The Vite proxy sends `/api` to `http://127.0.0.1:8000`, so `VITE_API_URL` stays
relative and there is no CORS hop in development.

---

## Quick start

```bash
# 1. Dependencies
pip install -r requirements.txt

# 2. Database (PostgreSQL 15+)
createdb evenmore_erp
psql -d evenmore_erp -c "create extension if not exists pgcrypto;
                         create extension if not exists citext;
                         create extension if not exists pg_trgm;
                         create extension if not exists btree_gin;"

# 3. Configuration
cp .env.example .env          # then set DB_PASSWORD

# 4. Schema and demo data
python manage.py migrate
python manage.py seed_demo

# 5. Verify
python manage.py smoke_test   # 15 end-to-end checks over real HTTP

# 6. Run
python manage.py runserver
```

Sign in with `admin@sweven.test` / `Sweven@2026`.

| URL | What |
|---|---|
| `http://localhost:8000/api/v1/` | API root |
| `http://localhost:8000/api/v1/docs/` | Swagger UI |
| `http://localhost:8000/api/v1/schema/` | OpenAPI 3.1 document |

---

## Layout

```
config/            settings, root URLconf at /api/v1
apps/
  core/            tenancy, error contract, numbering, idempotency, audit,
                   money, files, notifications, settings, search, dashboard
  accounts/        tenants, users, roles, permissions, sessions, auth (api.md §2-3)
  masters/         parties, items, categories, units, locations, BOM (api.md §4)
  sales/           estimate -> quotation -> order -> challan -> invoice -> payment (§5)
  purchase/        PO -> GRN/QC -> bill -> payment out, expenses (§6)
  inventory/       the movement ledger, transfers, RMA, zones, audits (§7)
  accounting/      chart of accounts, journal, ledgers, financial reports (§8)
  crm/             leads, stage automation, deals, contracts, forms (§9)
  pms/             projects, stages, tasks, documents, approvals, delays (§10)
  hrms/            employees, attendance, leave, payroll, recruitment … (§11)
  reports/         report catalogue, exports, dashboard, public endpoints (§12)
```

Each app is `models / serializers / views / urls`, plus `services.py` where the
business rules live. **The services are the interesting part** — views are thin
and mostly declarative.

---

## The rules that shaped the code

Everything below is a spec requirement that changed a design decision, not a
preference.

**The server is authoritative for every number.** api.md §5.7 ports the totals
algorithm verbatim from `ERPContext.createInvoice`; it lives in
`apps/core/money.py` and is re-run on create, on every draft edit and again at
finalization. Client-supplied totals are recomputed and rejected beyond a 0.01
tolerance. The header check constraint from db.md §3.1 is installed on all
twelve document tables, so a bug in the recompute is a loud failure rather than
a wrong ledger.

**Stock is a ledger, not a column.** `stock_movements` is append-only;
corrections are new reversing movements linked through
`original_movement`/`reversal_movement`. `availableQty`, `reservedQty` and
`status` are *not* columns on `items` — they are derived in
`apps/inventory/services.calculate_item_stock`, which is
`ERPContext.calculateItemStock` moved server-side. Weight-tracked items use
`coalesce(weighed_qty, quantity)`, so a steel receipt records the number the
weighbridge showed.

**Money and stock post together or not at all.** Finalizing an invoice
allocates the number, recomputes totals, posts `SALE` movements (skipping lines
a challan already depleted), posts Dr Debtors / Cr Sales / Cr GST, and updates
the party balance and order rollup — in one transaction.

**Derived values are never stored where they can go stale.** `Overdue`,
warranty `Expired`, contract `Expiring Soon`, HR document `Valid/Expiring
Soon/Expired` and PMS at-risk are all computed at read (db.md §12). Storing
them would need a cron job that is always a day wrong in some timezone.

**The GST split reads the company profile.** The frontend hardcodes Maharashtra;
`apps/sales/services.is_intra_state` compares the party's `place_of_supply`
against `company_profile.state`.

**The handoff gate is one function.** api.md §10.3 and db.md §10.5 both insist
`GET …/handoff-check/` and `POST …/handoff/` run the same query;
`apps/pms/services.handoff_blockers` is called by both, and returns blockers
**as data** (`{ code, label, hard }`), not as errors.

**CRM automation runs server-side.** `leadStageAutomation.js` and
`taskCompletionService.js` are `apps/crm/services.py`. A stage change returns
`{ lead, createdTasks: [] }` so the UI can toast what was generated, and the
role→user roster comes from `users.crm_roles` (`GET /crm/team-roster/`) rather
than the hardcoded `CRM_TEAM_MEMBERS`.

**Tenancy has two layers.** Every queryset is narrowed by
`TenantScopedMixin`; `manage.py enable_rls` adds Postgres RLS policies keyed on
`app.client_id`, which authentication publishes per request. Cross-tenant reads
return 404, never 403 (api.md §1.11).

---

## Commands

| Command | Purpose |
|---|---|
| `seed_demo [--reset]` | Demo tenant built by posting real documents, so every derived number has a cause (db.md §14.1) |
| `smoke_test` | The api-integration.md §12.3 checklist over real HTTP |
| `run_maintenance --job {hourly,nightly,all}` | The db.md §13 jobs. Reconcilers **alert, never auto-correct** |
| `enable_rls [--dry-run] [--drop]` | Install row-level security on all 160 tenant tables |

`run_maintenance` exits non-zero when it finds drift, so it can be wired
straight into an alerting cron.

---

## Frontend integration

The backend satisfies the blockers listed in api-integration.md §2:

- CORS allows the dev origin plus the `Authorization` and `Idempotency-Key`
  headers (`CORS_ALLOWED_ORIGINS` in `.env`).
- `POST /auth/refresh/` supports the single-flight silent refresh of §5.2;
  an expired token returns **401**, never 403, so the client's
  clear-and-redirect works.
- Every financial `POST` accepts `Idempotency-Key`; a replay returns the
  original `201` body byte-for-byte.
- Every mutable entity returns `updatedAt` and accepts `If-Unmodified-Since`
  or `expectedVersion`; a conflict is `409` with the current server copy in
  `payload`.
- The aggregate endpoints §9.1.3 asks for exist, so the cross-collection
  widgets can be migrated *before* their lists are paginated:
  `/parties/{id}/summary/`, `/parties/{id}/documents/`, `/search/`,
  `/purchase/orders/auto-suggestions/`, `/inventory/items/{id}/stock/`.

Point the Vite dev proxy at `http://localhost:8000` and leave `VITE_API_URL`
relative, per api-integration.md §3.2.

**What the app reads and writes.** `backendSync.js` covers parties (and their
customer/vendor projections), categories, units, locations, items, the seven
sales documents, the three purchase documents, payments in and out, and
expenses. Everything else in `ERPContext` — PMS, HRMS, CRM stores, stock
transfers, journal entries, warranties — is still local state; the endpoints
exist, the mapping does not yet.

Three contract bugs surfaced while wiring it up and were fixed here, not
worked around in the client:

- `code` was required on party, category and location creates, so nothing the
  wizards posted could be accepted. The server allocates `CUST-`/`VEND-`
  (§1.7); category and location codes are derived from the name when omitted.
- `PurchaseOrder`/`PurchaseBill`/`PurchaseReturn` declared `vendorId` *over*
  the base serializer's `partyId`, both writable on the same source, so DRF
  demanded both. `partyId` is now a read-only mirror.
- The challan and stock-transfer viewsets had an `@action` named `dispatch`,
  which shadows `APIView.dispatch` and broke every request to those endpoints
  with a 500. Renamed, URL unchanged.

Plus two additions the client needed: `companyProfile.currency` (api.md §1.6
refers to it; the profile did not expose it) and the payment-mode enum widened
to the strings the UI's `<select>` actually emits (`Bank Transfer`, `Bank
Wire`, `ACH`, `Corporate Card`).

---

## Not built

Stated plainly rather than stubbed silently:

- **PDF rendering.** `/…/print/` returns the full JSON print payload (company
  profile, document, totals in words). `/…/pdf/` returns `501
  PDF_RENDERER_UNAVAILABLE` rather than a broken file. Wiring WeasyPrint or a
  headless renderer is a drop-in at `apps/core/printing.py`.
- **Outbound email / WhatsApp.** `/…/send/` with `channel=email` delivers
  for real through Django SMTP (`EMAIL_HOST`, `EMAIL_PORT`,
  `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`); without
  those it fails with a clear `EMAIL_NOT_CONFIGURED` error instead of fake
  success. WhatsApp has no provider: `/…/send/` records the intent and says
  so (`"sent": False`). Password-reset tokens are logged, not mailed.
- **FIFO valuation.** WAC is implemented from movement unit costs (db.md
  Appendix B decision 5). FIFO needs a `stock_layers` table, so `?method=FIFO`
  reports that it is unavailable instead of quietly returning WAC.
- **xlsx / pdf export.** CSV works; the other two formats return a clear error
  rather than a mislabelled CSV.
- **Object storage.** Files use a local backend behind the same signed
  three-step flow (`upload-url` → `PUT` → `commit`), so swapping in S3 is
  confined to `apps/core/files.py`.
- **e-invoicing (IRN / e-way bill)** — api.md §5.7 marks it phase 2.
- **`audit_log` partitioning** — the table and its indexes match db.md §2.5;
  monthly partitioning is a migration to add before the table gets large.
