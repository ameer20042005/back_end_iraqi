# Jenni Stores — Database Schema for AI Query Builder

> **Purpose:** Feed this document to an AI SQL/query builder as the system schema for **Jenni Stores** (`back-end-microservices`).
>
> **Scope:** Physical tables derived from JPA `@Entity` classes across all microservices.
>
> **Generated from:** Java entities in the monorepo (not live DB introspection). Column naming follows Spring Boot default CamelCase → `snake_case` unless `@Column(name=…)` / `@Table(name=…)` overrides.

---

## 0. Global rules (MUST apply when generating SQL)

1. **Multi-tenant:** Almost every business table has `tenant` (`varchar`). Always filter `tenant = :tenant` unless the user explicitly asks across tenants (platform-only).
2. **Soft delete:** Entities extending `BaseEntity` have `deleted boolean` (default `false`). Prefer `deleted = false` (or `deleted IS NOT TRUE`).
3. **Audit columns** (on most tables): `created_by`, `created_at`, `updated_by`, `updated_at`.
4. **Separate databases per service.** Do **not** invent cross-DB SQL JOINs unless the environment has a warehouse / FDW. Cross-service links are **logical IDs** only.
5. **Enums** are stored as `varchar` (STRING), not Postgres enums.
6. **Money** is usually `numeric(19,2)` (IQD / USD as separate columns where applicable).
7. **JSON:** Several columns are `jsonb` / TEXT JSON (`delivery_info`, `profit_info`, `logistic_info`, etc.).

### Shared superclass (not a table)

| Logical fields | Description |
|----------------|-------------|
| `created_by`, `updated_by` | Who created/updated the row |
| `created_at`, `updated_at` | Timestamps (`timestamptz` / Instant) |
| `deleted` | Soft-delete flag |

### Prod DB naming pattern

| Service | Typical Postgres DB (prod) |
|---------|----------------------------|
| catalog | `jennisalesprod_catalog` |
| iam | `jennisalesprod_iam` |
| commondata | `jennisalesprod_commondata` |
| ledger | ledger DB (service-local; often `ledger_db` / env-specific) |
| tenantcontrol | tenantcontrol DB |
| reporting | `jennisalesprod_reporting` (+ reads catalog) |
| importer | `jennisalesprod_importer` |
| transporterintegration | `jennisalesprod_transporterintegration` |

Local profile often uses `*_db` (e.g. `catalog_db`).

---

## 1. Architecture map (services = databases)

```
tenantcontrol.company ──tenant──► all other services (logical)
iam.users ◄──user_id── catalog merchants/accountents/pos_users/...
ledger.wallet ◄──wallet_id── catalog partners + company
commondata.cities/districts ◄──city_id / delivery_info.districtId── catalog
catalog.sells ──► reporting.sell_summary (ETL copy)
catalog.sells/transporters ──► transporterintegration.shipments
```

---

## 2. tenantcontrol

**Role:** Company / tenant registry (one company = one tenant key).

### `company`
| Column | Type (approx) | Description |
|--------|---------------|-------------|
| `id` | bigint PK | Company id |
| `tenant` | varchar UK | Tenant key used everywhere else |
| `name` | varchar | Company display name |
| `status` | varchar | `ACTIVE` \| `DISABLED` |
| `subscription_type` | varchar | Subscription plan label |
| `wallet_id` | bigint | Logical FK → ledger `wallet.id` (company wallet) |
| `pos_active` | boolean | Whether POS module is enabled |
| + BaseEntity | | |

---

## 3. iam

**Role:** Authentication, users, roles, permissions, preferences.

### `users`
| Column | Description |
|--------|-------------|
| `id` | User PK |
| `tenant` | Tenant |
| `username` | Login name (unique per tenant) |
| `name` | Display name |
| `password` | Hash |
| `active` | Enabled flag |
| `role_id` | FK → `roles.id` |
| `work_shift` | `MORNING` \| `AFTERNOON` \| `FULL_SHIFT` |
| `email`, `job_title`, `phone_number` | Profile |
| `working_start_date` | Hire/start date |
| `city_id` | Logical → commondata `cities.id` |
| `user_image_url` | Profile image key/URL |

### `roles`
Tenant-scoped role (`admin`, `accountant`, `merchant`, …) with `name`, `name_arabic`.

### `permissions`
Authority string in `name` (e.g. `catalog:read`, `purchase:write`).

### `role_permissions`
Join `role_id` ↔ `permission_id`; `is_modifiable`.

### `permission_groups` / `permission_group_assignments`
UI grouping of permissions (global group codes + assignments).

### `refresh_tokens`
Refresh token hashes per user/tenant (`token_hash`, `expires_at`, `revoked`).

### `preferences` / `user_preferences`
Preference definitions + per-user values (`value_type`: STRING/NUMBER/INTEGER/BOOLEAN/SELECTION).

---

## 4. commondata

**Role:** Shared geo + company settings + files metadata (settings live here).

### `countries`
ISO country per tenant: `code`, `code3`, `numeric_code`, `name`, `name_arabic`, `phone_code`, `active`.

### `cities`
Governorate/city under country: `country_id` → `countries`, `name`, `name_arabic`, `code` (e.g. `BGD`), delivery invoice prices, `active`, `tenant`.

### `districts`
Neighborhood under city: `city_id` → `cities`, `name`, `rural`. (**No `tenant` column.**)

### `setting`
Setting **definition** per tenant: `name` (persistence key), `value_type` (`BOOLEAN`\|`STRING`\|`INTEGER`), `default_value`, EN/AR labels, `is_system`.

Important names include:
- `allow_online_sell_when_out_of_stock`
- `sell_receipt_number_prefix`
- `company_logo`
- `landing_page_counts` (system; default `1`)

### `company_setting`
Per-tenant **value**: `setting_id` → `setting.id`, `tenant`, `value` (TEXT).

---

## 5. ledger

**Role:** Wallets, transfers, expenses, audit balances, debt-related txn names.

### `wallet`
| Column | Description |
|--------|-------------|
| `id` | Wallet PK |
| `tenant` | Tenant |
| `owner_type` | `COMPANY` \| `PROVIDER` \| `TRANSPORTER` \| `MERCHANT` \| `EMPLOYEE` \| `ACCOUNTANT` \| `ACCOUNTANT_MANAGER` \| `ADMIN` |
| `owner_id` | Owner business id (catalog/IAM depending on type) |
| `owner_name` | Cached display name |
| `iqd_balance`, `usd_balance` | Balances |
| `version` | Optimistic lock |

UK: `(tenant, owner_type, owner_id)`.

### `txn` (entity name `Txn`)
Wallet ledger line.
| Column | Description |
|--------|-------------|
| `wallet_id` | FK → `wallet` |
| `tenant` | Tenant |
| `direction` | `WITHDRAW` \| `DEPOSIT` |
| `type` | High-level type (expenses, clearing, settlements, …) |
| `transaction_name` | Specific named action (e.g. `COMPANY_LENDS_CASH_DEBT`, `EXPENSE_RENT`) |
| `amount_iqd`, `amount_usd` | Movement amounts |
| `balance_after_iqd`, `balance_after_usd` | Snapshot after txn |
| `related_wallet_id` | Counterparty wallet (debts/transfers) |
| `reference_id` | Pairs related withdraw+deposit |
| `external_reference_id` | External/business ref |
| `expense_date`, `attachment_url` | Expense fields |
| `description`, `description_arabic` | Notes |
| `audit_balance_id` | Optional FK → audit |

### `transfer_reference`
Sequence/counter rows used as `txn.reference_id`.

### `audit_balance`
Cash-count vs system balances per wallet; IQD/USD status `MATCHED` \| `CORRECTED`.

---

## 6. catalog (largest / core commerce DB)

**Role:** Products, stock, purchases, sells (POS+online), partners, settlements, sell-flow.

### 6.1 Partners / access

| Table | Description | Key links |
|-------|-------------|-----------|
| `merchants` | Merchant partner | `user_id`→IAM, `wallet_id`→ledger; profits; `logistic_info` jsonb |
| `merchant_storages` | M2M merchant↔storage | |
| `merchant_categories` | M2M merchant↔category | |
| `merchant_product_profits` | Per-product profit override | `merchant_id`, `product_id` |
| `pages` | Online sales page under merchant | `merchant_id` |
| `page_social_medias` | Page social channels | enum name INSTAGRAM/… |
| `page_categories` | M2M page↔category | |
| `page_product_profits` | Page product profit override | |
| `page_employees` | Page staff | `user_id`, `wallet_id` |
| `page_employee_pages` | M2M employee↔pages | |
| `accountents` | Accountant profile | `user_id`, `wallet_id`, storage scope |
| `accountent_storages` | M2M | |
| `general_users` | Generic catalog user + storage scope | |
| `general_user_storages` | M2M | |
| `providers` | Purchase suppliers | `city_id`, `wallet_id` |
| `provider_categories` | M2M | |
| `transporters` | Delivery companies + fee schedule | `wallet_id`, `logistic_integration_active` |

### 6.2 Catalog & inventory

| Table | Description |
|-------|-------------|
| `categories` | Category tree (`parent` self-FK), image, sort, pretty_name |
| `products` | Product master: SIMPLE/VARIABLE, prices, sku/barcode, photos JSON, `deleted_at` |
| `product_categories` | M2M products↔categories |
| `product_variants` | Variants of VARIABLE products (`product_id`) |
| `attributes` / `attribute_values` | Variant attributes |
| `product_variant_attribute_values` | Variant ↔ attribute_value |
| `storages` | Warehouses (`city_id`, `active`) |
| `stock_info` | Qty per product/variant × storage × expire_date; damaged qty; purchase_price_info jsonb |
| `stock_logs` | Stock movements (`how_changed`: AUTOMATIC/MANUAL/SELL/SELL_RETURN/PURCHASE/RECOVERY) |
| `damage_reasons` | Damage reason dictionary |
| `generated_barcodes` | Generated barcode registry |

### 6.3 POS

| Table | Description |
|-------|-------------|
| `pos_stores` | POS store linked to a storage |
| `pos_store_categories` | M2M |
| `pos_users` | Cashier / cashier-manager (`role_type`), `user_id`, `pos_store_id` |

### 6.4 Purchases

| Table | Description |
|-------|-------------|
| `purchases` | Purchase header: `provider_id`, `storage_id`, `purchase_date`, `status` DRAFT\|SUCCESS\|DELETE, totals |
| `purchase_items` | Lines: qty, unit_purchase_price, product/variant, `stock_info_id` |

### 6.5 Sells (core)

### `sells`
Central order table (POS + ONLINE).

| Column / group | Description |
|----------------|-------------|
| `id`, `tenant`, `receipt_number` | Identity (UK tenant+receipt) |
| `sale_type` | `POS` \| `ONLINE` |
| `sell_status` | `DRAFT` \| `SUCCESS` \| `DELETE` \| `PENDING` |
| `payment_method` | `CASH` \| `CREDIT_CARD` \| `CASH_ON_DELIVERY` |
| `payment_status` | `PAID` \| `NOT_PAID` |
| `customer_name`, `customer_phone_number` | Customer |
| `total_price`, `total_net_amount`, `total_discount`, `receipt_discount` | Money |
| `creator_user_id/name/role` | Who created |
| `current_step_id` | FK → `sell_flow_step` (workflow state) |
| `page_id`, `page_social_media_id` | Online attribution |
| `storage_id`, `pos_store_id` | Fulfillment / POS |
| `assigned_transporter_id` | Delivery company |
| `delivery_info` | jsonb (includes `districtId`, address fields) |
| `profit_info`, `logistic_info` | jsonb |
| settlement info jsonb | transporter/merchant/page-employee settlement snapshots |
| `manifest_number/date`, `return_manifest_*` | Delivery manifests |
| `print_manifest_number` | Logical FK → `print_manifests.id` |
| `special_sell`, `sticker_printed`, `estimated_delivery_date` | Flags/dates |
| `remarks` | Notes |

### Related sell tables

| Table | Description |
|-------|-------------|
| `sell_items` | Line items: qty, unit prices/discounts/net, `stock_info_id`, `profit_info` |
| `sell_logs` | Human-readable sell activity log |
| `sell_item_returns` | Returns: qty + reason enum |
| `print_manifests` | Batch print/manifest group (transporter + page) |

### 6.6 Settlements

| Table | Description |
|-------|-------------|
| `settlements` | Transporter cash settlement; amounts IQD/USD; `transporter_id`; `transaction_id` → ledger |
| `merchant_settlements` | Merchant payout for delivered/settled sells |
| `page_employee_settlements` | Page-employee profit payout |

### 6.7 Sell flow (workflow engine)

| Table | Description |
|-------|-------------|
| `sell_flow_stage` | Stages (ORDER_FULFILLMENT, OUT_FOR_DELIVERY, RETURN, FINISHED, …) |
| `sell_flow_step` | Steps inside stages (`ready_to_settle`, `requires_manifest_info`, …) |
| `sell_flow_transition` | Allowed from_step → to_step + `action_code` |
| `sell_flow_transition_log` | History of step changes per sell |

### Catalog join cheatsheet

```text
merchants.id ← pages.merchant_id
products ↔ categories (product_categories)
stock_info.product_id → products
stock_info.storage_id → storages
sells.id ← sell_items.sell_id
sell_items.stock_info_id → stock_info
sells.current_step_id → sell_flow_step.id → sell_flow_stage.id
sells.assigned_transporter_id → transporters.id
sells.page_id → pages.id
purchases.provider_id → providers.id
purchases.id ← purchase_items.purchase_id
```

Logical (other DBs):
- `*.user_id` / `creator_user_id` → `iam.users.id`
- `*.wallet_id` → `ledger.wallet.id`
- `*.city_id` → `commondata.cities.id`
- `sells.delivery_info->>'districtId'` → `commondata.districts.id`

---

## 7. reporting

**Role:** Analytics copy of sells + sync metadata.

### `sell_summary`
Denormalized sell mirror for reporting queries (`original_sell_id` → catalog `sells.id`). Prefer this DB for heavy analytics if ETL is up to date.

### `sell_sync_run`
Per-tenant sync cursor (`last_processed_at`, `last_run_at`, `last_batch_count`).

### `sells` (SellSource, catalog datasource)
Read-only mapping to **catalog** `sells` used by the sync job (not reporting’s primary schema for end-user queries).

---

## 8. importer

**Role:** Google Sheets → imported online sells.

### `sheet_configs`
Sheet connection per tenant (`google_sheet_url`, `spreadsheet_id`, `active`, `last_imported_at`).

### `imported_sells`
Imported rows: `status` (`INVALID`\|`NEW`\|`CONVERTED_TO_SUCCESSFUL_SELL`\|`REJECTED`\|`DELETED`), `dedup_hash`, `sell_id` (after conversion), `raw_row`, phone/date/note.

---

## 9. transporterintegration

**Role:** External logistics integration (Jenni/3PL).

| Table | Description |
|-------|-------------|
| `transporter_credential` | API credentials per tenant+transporter (`base_path`, username/password, `system_code`) |
| `shipments` | Latest shipment state; links `sell_id`, `transporter_id`, external ids |
| `create_shipment_log` | Outbound create-shipment request/response audit (jsonb) |
| `create_merchant_log` | Merchant upsert at logistics (`CREATED`\|`FOUND_EXISTING`) |
| `create_store_log` | Store/page upsert at logistics |
| `webhook_log` | Inbound webhook audit (**often no tenant column**) |

---

## 10. Suggested “query domains” for the AI

When the user asks a question, pick a **primary database** first:

| User intent | Primary DB / tables |
|-------------|---------------------|
| Orders, delivery status, customers, POS vs online | **catalog** `sells`, `sell_items`, `sell_flow_*` |
| Inventory / stock | **catalog** `stock_info`, `stock_logs`, `products` |
| Purchases / suppliers | **catalog** `purchases`, `purchase_items`, `providers` |
| Merchants / pages / profits | **catalog** `merchants`, `pages`, `*_profits` |
| Settlements with transporters/merchants | **catalog** `settlements`, `merchant_settlements` |
| Wallets, expenses, debts, transfers | **ledger** `wallet`, `txn` |
| Users / roles | **iam** `users`, `roles`, `permissions` |
| Cities / districts / company settings | **commondata** |
| Company list / subscription | **tenantcontrol** `company` |
| Analytics over sells | **reporting** `sell_summary` (fallback catalog `sells`) |
| Sheet imports | **importer** |
| Logistics tracking / webhooks | **transporterintegration** |

---

## 11. Example natural-language → SQL sketches

**Q:** “How many successful online sells this month for tenant X?”  
**DB:** catalog  
```sql
SELECT COUNT(*)
FROM sells
WHERE tenant = :tenant
  AND deleted = false
  AND sale_type = 'ONLINE'
  AND sell_status = 'SUCCESS'
  AND created_at >= date_trunc('month', now());
```

**Q:** “Top products by purchased quantity”  
**DB:** catalog  
```sql
SELECT pi.product_id, pi.product_name, SUM(pi.quantity) AS qty
FROM purchase_items pi
JOIN purchases p ON p.id = pi.purchase_id
WHERE p.tenant = :tenant
  AND p.deleted = false
  AND pi.deleted = false
  AND p.status = 'SUCCESS'
GROUP BY pi.product_id, pi.product_name
ORDER BY qty DESC;
```

**Q:** “Company wallet IQD balance”  
**DB:** ledger  
```sql
SELECT iqd_balance, usd_balance
FROM wallet
WHERE tenant = :tenant
  AND deleted = false
  AND owner_type = 'COMPANY';
```

---

## 12. Entity count summary

| Service | Approx. entities/tables |
|---------|-------------------------|
| catalog | ~37 (+ join tables) |
| iam | 9 |
| transporterintegration | 6 |
| commondata | 5 |
| ledger | 4 |
| reporting | 3 |
| importer | 2 |
| tenantcontrol | 1 |
| **Total** | **~67 entities** |

---

## 13. Notes for AI implementers

1. Always ask/resolve **which tenant** before querying.
2. Prefer **reporting.sell_summary** for heavy sell analytics if sync lag is acceptable; otherwise **catalog.sells**.
3. Never JOIN across microservice DBs in one SQL statement unless a unified warehouse exists.
4. Treat `wallet_id`, `user_id`, `city_id`, `transaction_id` as **logical foreign keys**.
5. For debts/expenses reporting, use ledger `txn.transaction_name` / `type` filters (domain-specific enums in utilslib `TransactionName`).
6. JSON paths example: `sells.delivery_info->>'districtId'`.

---

*End of schema pack. Keep this file updated when entities change.*
