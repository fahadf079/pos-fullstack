# POS — Real-Time Inventory Point of Sale

A web-based Point-of-Sale (POS) and inventory management system for a grocery store / mart.

The system is designed around one central idea: **sales, stock, purchasing, refunds, expenses, user actions, and reporting all stay connected to the same live inventory and audit trail.**

It runs locally and uses a **React + TypeScript** frontend, a **Python + FastAPI** backend, and **PostgreSQL** for persistent data.

> **Status: Prototype v1.0 (pilot / MVP).** The logic, security model and data model are complete for the pilot in the mart. The interface is being finished separately, so screens are functional first. A fresh installation has no people: the first screen creates the owner.

---

## Product Overview

This POS is more than a checkout screen. It covers the main operational flow of a small retail store:

**Sell → Inventory changes → Purchase/receive stock → Track every movement → Manage invoices/refunds → Record expenses → Review reports → Audit user activity**

The application keeps these areas connected so that a stock change made in one part of the system is reflected in the relevant inventory, movement history, invoices, and reporting views.

---

## Main Capabilities

### 🛒 Point of Sale / Selling

The selling screen is the main operational workspace.

- Products can be found by **barcode/SKU** or by searching their name.
- Products can be sold as whole units or as **weighed quantities** such as kg/litre, with up to 3 decimal places.
- Inventory is deducted **when an item is added to the cart**, rather than waiting until the final checkout.
- Removing an item or clearing the cart returns the reserved stock.
- The cart supports item quantities, live pricing and totals.
- Checkout supports:
  - Cash
  - Card
  - Wallet
- Tax is applied to the sale and discount codes can be used where permitted.
- The receipt records the person who served the customer.
- Refunds can be performed per item or partially, with the corresponding stock restored.

This makes the POS screen directly connected to inventory rather than treating sales and stock as separate systems.

### 📦 Real-Time Inventory

Inventory is handled through a central stock-change flow.

Every stock change records:

- What changed
- Previous quantity
- New quantity
- Reason/type of movement
- User responsible
- Related business action

Examples of stock movements include:

- Sale / scan
- Cart removal / void
- Purchase receiving
- Manual stock adjustment
- Refund

The application prevents stock from becoming negative through application-level checks as well as a database constraint.

The frontend also receives live updates through **Server-Sent Events (SSE)** so open screens can reflect inventory changes without manually refreshing.

### 🏷️ Product Catalog

The Catalog section manages the products available to the POS.

It supports:

- Adding products
- Editing product information
- Product price and cost
- Live margin calculation
- Below-cost warnings
- Product activation/deactivation
- Immutable barcode/SKU
- Product price history

Price and cost changes are controlled actions: the owner needs no extra step; an employee-level login cannot reach the Catalog at all.

### 🚚 Purchasing & Stock Receiving

The purchasing workflow connects supplier purchasing directly to inventory.

It covers:

- Suppliers
- Purchase orders
- Creating purchase orders
- Cancelling/closing purchase orders
- Receiving stock, including partial receiving
- Barcode-based receiving
- Recording purchases as paid/unpaid
- Supplier balances
- Purchase reporting

When stock is received, inventory is increased through the same controlled stock movement system used by the POS.

This keeps the relationship between **what was purchased, what was received, and what is currently in stock** traceable.

### 🧾 Invoices & Refunds

Sales are retained as invoices that can be reviewed after checkout.

The invoice workflow supports:

- Viewing completed sales
- Reviewing line items
- Tracking quantities and totals
- Partial refunds
- Per-item refunds
- Restoring refunded quantities to inventory

Refunds are also reflected in the audit/history trail so the original sale and subsequent adjustment remain traceable.

### 💰 Expenses & Reports

Managers can record operating expenses and review business activity through the reporting area.

The reports cover information such as:

- Net sales
- Refunds
- Purchases
- Expenses
- Cash flow
- Unpaid supplier amounts
- Top-selling products

Expenses are not silently deleted. A voided expense remains part of the operational history together with the reason for the void.

### 🕵️ History / Audit Log

The History section provides an audit trail of business activity.

Business events are recorded in the same transaction as the underlying change, so the system can connect an action with the data change it caused.

History can be filtered by:

- Event type
- Text
- Date

The audit information identifies the person who performed the action.

The history and stock ledgers are designed to be **append-only**, which helps preserve an operational record rather than allowing business events to simply disappear.

---

## User Roles & Access Control

| Role | Signs in with | Can do |
|---|---|---|
| **Employee** | Name tile + own PIN (no password) | Selling, products/stock visibility, invoices, stock ledger, own cash-up shift. Refunds, discount codes and stock counts need the employee's **own PIN**. |
| **Owner** (managers count as owners) | One password, plus 2FA once switched on | Everything: Catalog, Purchasing, Expenses, Reports, History, people (add, rename, remove employees) & PINs, Settings, Network, Cash up reports. Needs no PIN for actions: the password (and 2FA) at sign-in already proved who they are. |
| **Developer** | Separate password + mandatory 2FA, created only from the command line | Everything, plus the network-protection override and diagnostics. Never listed or editable in the app. |
| **Guest** | Nothing | Read-only demo (Dashboard, Inventory). Off unless the owner switches it on in Settings; ends after 15 minutes. |

Only an owner can add people, rename them (name and username), remove an employee, set or change an employee's PIN, reset another owner's password, or unlock a locked account. A removed employee can no longer sign in; their past sales and History keep the name they had at the time.
Every permission lives in one table (`backend/policy.py`); the server refuses to start if a route has no rule.

## Authentication & Security

- **Ordered request pipeline** (`backend/pipeline.py`): network gate → identity → lock → permission → PIN → action → record. Safety checks always run first.
- **1-minute inactivity lock**: the screen is covered and the server refuses everything until the same person signs in again (employee: PIN, owner: password). The sale is not touched. The server also locks by itself if it hears nothing for 80 seconds.
- **2FA** for owners (authenticator app, 8 one-time recovery codes); mandatory for the developer. Recovery: `python manage_users.py reset-2fa NAME` on the POS computer.
- **Network protection**: only this computer and the shop's private network may connect; the owner can pin the POS to one approved connection in Settings → Network. A critical alert appears if that connection goes down.
- **Critical alerts** need an explicit acknowledgement (who and when are recorded) and stay until the problem is fixed.
- **Cash drawer** opens only for a saved cash sale or a recorded manual opening; **cash-up** makes the employee count before the system works out the expected amount.
- **Card-machine credentials** are write-only: stored sealed, never sent to the screen or written to logs.
- **History is append-only** (database triggers and, with `secure_db.py`, permissions).
- Passwords and PINs are salted `scrypt` hashes; PINs also use a secret key kept outside the database (`backend/pos_secret.key`: back it up separately).


## How the System Works

The application follows a clear separation between the UI, API/business logic and database.

```mermaid
flowchart LR
    A[React / TypeScript POS] -->|HTTP API| B[FastAPI Backend]
    A <-->|Server-Sent Events| B
    B --> C[Authentication & Permissions]
    B --> D[POS / Catalog / Purchasing]
    B --> E[History & Stock Ledger]
    B --> F[PostgreSQL]
    D --> F
    E --> F
```

### Typical Sale Flow

```text
Product scan/search
        ↓
Product + quantity added to cart
        ↓
Available stock checked
        ↓
Stock reserved/deducted
        ↓
Cart can be changed
        ↓
Removed items return to stock
        ↓
Checkout
        ↓
Invoice created
        ↓
History/audit event recorded
        ↓
Reports and invoice data updated
```

### Typical Purchase Flow

```text
Supplier
   ↓
Purchase Order
   ↓
Order items
   ↓
Partial/full receiving
   ↓
Stock increases
   ↓
Stock movement recorded
   ↓
Purchase recorded as paid/unpaid
   ↓
Purchasing reports updated
```

### Refund Flow

```text
Existing Invoice
      ↓
Select item/quantity to refund
      ↓
Employee's own PIN when required
      ↓
Refund recorded
      ↓
Inventory restored
      ↓
History updated
```

---

## Live Inventory & Audit Model

One of the important design choices is that stock changes are not scattered across unrelated parts of the application.

The system uses a central stock movement model so actions such as selling, receiving, adjusting and refunding can be traced consistently.

Conceptually:

```text
Business Action
      ↓
Stock Change
      ↓
Before / After Quantity
      ↓
Movement Ledger
      ↓
History / Audit Event
```

This gives the system a traceable relationship between an operational action and the inventory change it produced.

---

## Database

PostgreSQL is used as the primary database.

The database stores the application's operational data, including:

- Users and roles
- Products
- Inventory
- Sales/invoices
- Cart state
- Stock movements
- Purchase orders
- Suppliers
- Purchases
- Expenses
- History/audit events
- Product price history

The backend uses transactions for business operations so related changes can be committed together.

A restricted application database login can also be configured so the POS application itself cannot modify or delete protected history/ledger data.

---

## Screenshots

The following screenshots show the current application flow and major areas of the POS.

### Point of Sale

| Dashboard | Cart / Sale |
|---|---|
| ![Dashboard](docs/screenshots/01-dashboard-empty.png) | ![Dashboard cart](docs/screenshots/02-dashboard-cart.png) |

| Payment / Cart | Weighted Item |
|---|---|
| ![Payment](docs/screenshots/03-dashboard-cart-payment.png) | ![Weighted item](docs/screenshots/05-dashboard-weighted-item.png) |

| Search / Item Selection | Checkout |
|---|---|
| ![Item search](docs/screenshots/06-dashboard-item-search.png) | ![Checkout](docs/screenshots/07-checkout.png) |

### Inventory

| Inventory | Low Stock |
|---|---|
| ![Inventory](docs/screenshots/04-inventory.png) | ![Low stock](docs/screenshots/09-inventory-low-stock.png) |

| Stock Warning | Stock Movements |
|---|---|
| ![Low stock warning](docs/screenshots/08-low-stock-warning.png) | ![Stock movements](docs/screenshots/10-stock-movements.png) |

### Catalog

| Catalog | Product Editing |
|---|---|
| ![Catalog](docs/screenshots/11-catalog.png) | ![Catalog edit](docs/screenshots/12-catalog-edit.png) |

| Product Form | Updated Catalog |
|---|---|
| ![Product form](docs/screenshots/13-catalog-product-form.png) | ![Catalog update](docs/screenshots/14-catalog-update.png) |

### Purchasing

| Purchase Orders | Create Purchase Order |
|---|---|
| ![Purchasing](docs/screenshots/15-purchasing-orders.png) | ![Create purchase order](docs/screenshots/16-purchase-order-create.png) |

| Purchase Order List | Receiving |
|---|---|
| ![Purchase orders](docs/screenshots/17-purchase-orders-list.png) | ![Receiving](docs/screenshots/18-purchase-receiving.png) |

### Approval, Invoices & Reporting

| Manager Approval | Purchasing History |
|---|---|
| ![Manager PIN approval](docs/screenshots/19-manager-pin-approval.png) | ![Purchasing history](docs/screenshots/20-purchasing-history.png) |

| Invoices | Invoice / Refund |
|---|---|
| ![Invoices](docs/screenshots/21-invoices.png) | ![Invoice refund](docs/screenshots/22-invoice-refund.png) |

| Expenses & Reports | Reports |
|---|---|
| ![Expenses and reports](docs/screenshots/23-expenses-reports.png) | ![Reports](docs/screenshots/24-reports.png) |

### Audit & Security

| History | Security |
|---|---|
| ![History](docs/screenshots/25-history-audit-log.png) | ![Security](docs/screenshots/26-security.png) |

### Cash, Reports & Settings

| Cash Up | End of Day |
|---|---|
| ![Cash up](docs/screenshots/Screenshot%202026-10-06%20141147.png) | ![End of day](docs/screenshots/Screenshot%202026-10-06%20141202.png) |

| Profit | Settings |
|---|---|
| ![Profit](docs/screenshots/Screenshot%202026-10-06%20141208.png) | ![Settings](docs/screenshots/Screenshot%202026-10-06%20141217.png) |

| Network & Card Machine | Receipt Print |
|---|---|
| ![Network and card machine](docs/screenshots/Screenshot%202026-10-06%20141222.png) | ![Receipt](docs/screenshots/Screenshot%202026-10-06%20141125.png) |

| Invoices | People & Security |
|---|---|
| ![Invoices](docs/screenshots/Screenshot%202026-10-06%20141318.png) | ![People and security](docs/screenshots/Screenshot%202026-10-06%20141233.png) |

Names shown in the screenshots are test data.

---

## Technology Stack

### Frontend

- React
- TypeScript
- Vite
- CSS
- Server communication through the backend API
- Server-Sent Events for live updates

### Backend

- Python
- FastAPI
- PostgreSQL database integration
- Transaction-based business operations
- Authentication and authorization
- Audit/history recording

### Testing

The project includes backend test suites covering:

- POS/business flows
- Selling
- Refunds
- Purchasing
- History
- Concurrency
- Authentication
- Roles and permissions
- Own-PIN confirmations
- Scanner and card-sale check with dummy values (`backend/tests/check_scan_and_card.py`)
- People management: rename and remove (`backend/tests/test_people.py`)
- Lockouts
- Sessions
- CORS
- Restricted database access

There is also a simulated-browser UI click-through covering the main user interface flows.

---

## Local Setup

The application is intended to run locally.

### Requirements

- Python
- Node.js / npm
- PostgreSQL

Docker can also be used for the PostgreSQL database.

### Database

Create the PostgreSQL database and application user, then configure the backend environment using:

```text
backend/.env.example
```

The main application database URL is configured through:

```text
DATABASE_URL
```

The default shop timezone is:

```text
Asia/Karachi
```

### First Run

On the first launch there are no people at all: the application shows a first-time setup screen to create the owner account (username and password).

After that, the owner adds employees (name + PIN) and, if wanted, other owners (password) through the Security section, and switches on 2FA under Account.

---

## Backups & Data Migration

The project includes database backup and migration functionality.

### Backups

The backup system supports:

- PostgreSQL dumps
- Verification after backup creation
- Retention of recent backups
- Scheduled daily backups
- Restore from a database dump

The application secret used for PIN protection is kept separately from the database backup and should be backed up separately as well.

### SQLite → PostgreSQL Migration

An existing SQLite database can be migrated to PostgreSQL.

The migration process is designed to preserve IDs and historical information and then verify:

- Row counts
- Money totals
- Stock ledger consistency

A dry-run mode is available before applying the migration.

---

## API Structure

The backend exposes REST-style API routes grouped by business area.

| Area | Examples |
|---|---|
| Authentication | `/auth/status`, `/auth/setup`, `/auth/login`, `/auth/logout` |
| Users & Security | `/users`, `/users/{id}/update`, `/users/{id}/remove`, `/security/policy` |
| Selling | `/inventory`, `/scan`, `/cart`, `/checkout`, `/sales`, `/movements` |
| Catalog | `/catalog/meta`, `/catalog/products`, `/catalog/products/{id}/update` |
| Purchasing | `/suppliers`, `/purchase-orders`, `/purchases`, `/expenses`, `/reports/purchases` |
| History | `/history`, `/history/meta` |

All protected routes require authentication, and non-GET requests use the application's request protection header.

---

## Project Structure

```text
pos-fullstack/
├── backend/
│   ├── main.py
│   ├── database.py
│   ├── auth.py
│   ├── catalog.py
│   ├── purchasing.py
│   ├── history.py
│   ├── backup.py
│   ├── secure_db.py
│   ├── init_db.py
│   ├── manage_users.py
│   ├── migrate_sqlite_to_postgres.py
│   └── tests/
│
├── frontend/
│   └── src/
│       ├── App.tsx
│       ├── api.ts
│       ├── auth.tsx
│       ├── CatalogPage.tsx
│       ├── PurchasingPages.tsx
│       ├── HistoryPage.tsx
│       └── SecurityPage.tsx
│
├── docs/
│   └── screenshots/
│
├── tests-ui/
│   └── ui-clickthrough.mjs
│
├── docker-compose.yml
└── README.md
```

---

## Current Implementation

The current implementation provides an end-to-end retail workflow covering:

- POS selling
- Real-time inventory
- Product catalog management
- Purchasing and stock receiving
- Invoices and refunds
- Expenses
- Business reports
- User roles and permissions
- Employee own-PIN confirmations
- People management for the owner (add, rename, remove employees)
- Cash drawer, cash-up, end-of-day and profit reports
- Authentication and security controls
- Audit/history tracking
- PostgreSQL persistence
- Backup and migration support
- Automated backend/security test coverage
- UI click-through testing

The screenshots above represent the current application interface and workflow.

---

## Notes

The application is currently designed for **local operation**. The existing implementation also documents several operational boundaries, including:

- HTTPS is optional (self-signed certificate via `make_cert.py`); plain HTTP is the default for local use.
- The application uses a single backend process because live-update state is maintained in-process.
- Weighed products accept typed weights or price-computing scale labels; there is no direct link to scale hardware.
- There is one global cart rather than separate carts per till.
- Real-world hardware such as barcode scanners, weighing scales and networked tills requires hardware-specific testing before deployment.

These are implementation boundaries of the current version rather than gaps in the core POS workflow.

---

## Summary

This project provides a complete local POS workflow in which **selling, inventory, purchasing, refunds, expenses, reporting, security and audit history work together around the same underlying data model**.

The result is a system where a retail transaction is not isolated: the sale affects stock, the stock change is recorded, the invoice remains available for review, refunds can restore inventory, purchasing can replenish stock, reports summarize activity, and History provides an audit trail of who performed and approved important actions.


## Recent additions
See `CHANGES-v12.md`. Quick start for HTTPS: in `backend\` run `python -m pip install cryptography` then `python make_cert.py --host <this computer's shop-network address>`, then double-click `start.bat` again and open `https://localhost:5173` (accept the one-time browser warning).
Other tills: set `POS_BIND_HOST=0.0.0.0` in `backend\.env`, allow ports 8000 and 5173 in the firewall, and use HTTPS first.
CSV import columns: `barcode,name,category,unit,price,cost` (unit pc / kg / l).
