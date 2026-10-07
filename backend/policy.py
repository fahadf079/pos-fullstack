"""
policy.py — the ONE table that says who may call what. Nothing else decides permissions.

  key = "METHOD /path"  ->  (lowest role allowed, PIN rule, what it is)
  roles   guest < employee < owner < developer
  PIN     None = no PIN;  "self" = an EMPLOYEE types their own PIN (owner/developer are exempt: they signed in with a password)

Every route must be listed here. `install(app)` is called last in main.py and refuses to start the app if a route has
no rule (or a rule names a route that does not exist). An unlisted route is also refused at run time (fail closed).
"""
from typing import Optional

POLICY: dict[str, tuple[str, Optional[str], str]] = {}

# reachable without signing in (still behind the network gate)
PUBLIC = {"GET /auth/status", "GET /auth/people", "POST /auth/setup", "POST /auth/login", "POST /auth/pin-login", "POST /auth/guest"}
# allowed while the screen is locked
LOCKED_OK = {"POST /auth/unlock", "POST /auth/logout"}
# an owner who still has to switch on 2FA (when the owner made it compulsory) may only use these
TWOFA_SETUP_OK = {"POST /auth/2fa/begin", "POST /auth/2fa/confirm", "POST /auth/logout", "POST /auth/activity", "POST /auth/lock", "POST /auth/unlock"}
# POSTs that must not count as "the person is active" (none today; kept explicit for the pipeline tests)
NO_TOUCH: set[str] = set()


def _p(method: str, path: str, role: str, pin: Optional[str] = None, label: str = "") -> None:
    POLICY[f"{method} {path}"] = (role, pin, label)


# what a guest may see (read-only demo)
for m_, p_, l_ in [("GET", "/inventory", "See products and stock"), ("GET", "/dashboard", "Dashboard"),
                   ("GET", "/cart", "See the cart"), ("GET", "/events", "Live updates")]:
    _p(m_, p_, "guest", None, l_)
# selling (everyone who works the till)
for m_, p_, l_ in [("POST", "/scan", "Scan / add an item to the cart"), ("POST", "/cart/remove", "Remove an item from the cart"),
                   ("POST", "/cart/set", "Change a cart quantity / weight"), ("POST", "/cart/clear", "Clear the cart"),
                   ("GET", "/sales", "See the invoice list"), ("GET", "/sales/{sale_id}", "Open / reprint an invoice"),
                   ("GET", "/movements", "See the stock ledger")]:
    _p(m_, p_, "employee", None, l_)
_p("POST", "/checkout", "employee", None, "Check out a sale (a discount code needs the employee's own PIN)")
_p("POST", "/sales/{sale_id}/refund", "employee", "self", "Refund (whole or part of an invoice)")
_p("POST", "/inventory/adjust", "employee", "self", "Correct stock after a physical count")
# alerts, cash drawer, shifts
_p("GET", "/alerts", "employee", None, "See active alerts")
_p("POST", "/alerts/{aid}/ack", "employee", None, "Acknowledge an alert (who and when is recorded)")
_p("GET", "/shifts/current", "employee", None, "My cash-drawer shift (never shows the expected cash)")
_p("POST", "/shifts/open", "employee", None, "Open my cash-drawer shift with the opening float")
_p("POST", "/shifts/close", "employee", None, "Close my shift by entering the counted cash")
_p("POST", "/shifts/move", "employee", "self", "Cash drop, payout or cash added during my shift (own PIN, reason required)")
_p("GET", "/shifts/moves", "owner", None, "Every cash drop / payout / addition")
_p("GET", "/reports/day", "owner", None, "End-of-day report: takings, refunds, shifts, cash")
_p("GET", "/reports/profit", "owner", None, "Profit report using what the shop paid for the goods")
_p("POST", "/drawer/open", "employee", "self", "Open the cash drawer by hand (reason required; owner must allow it for employees)")
_p("GET", "/shifts", "owner", None, "Cash-up report: expected cash and differences")
_p("POST", "/shifts/{sid}/review", "owner", None, "Review a cash difference")
_p("GET", "/drawer/events", "owner", None, "Every drawer opening and who caused it")
# catalog
for m_, p_, l_ in [("GET", "/catalog/meta", "Catalog: lists"), ("GET", "/catalog/products", "Catalog: see products"),
                   ("GET", "/catalog/products/{pid}/history", "Catalog: a product's change history"),
                   ("GET", "/catalog/next-barcode", "Catalog: an unused in-shop barcode"), ("POST", "/catalog/import", "Catalog: add many products from a file"),
                   ("POST", "/catalog/products", "Catalog: add a product"), ("POST", "/catalog/products/{pid}/update", "Catalog: edit a product"),
                   ("POST", "/catalog/products/{pid}/active", "Catalog: deactivate / reactivate a product")]:
    _p(m_, p_, "owner", None, l_)
# purchasing, suppliers, expenses, reports, history
for m_, p_, l_ in [("GET", "/purchasing/products", "Purchasing: product list"), ("GET", "/purchasing/meta", "Purchasing: lists"),
                   ("GET", "/suppliers", "See suppliers"), ("POST", "/suppliers", "Add a supplier"), ("POST", "/suppliers/{sid}/update", "Edit / deactivate a supplier"),
                   ("GET", "/purchase-orders", "See purchase orders"), ("GET", "/purchase-orders/{po_id}", "Open a purchase order"),
                   ("POST", "/purchase-orders", "Create a purchase order"), ("GET", "/purchases", "See purchases (deliveries)"),
                   ("GET", "/purchases/{rid}", "Open a purchase"), ("GET", "/expenses", "See expenses"), ("POST", "/expenses", "Add an expense"),
                   ("GET", "/reports/purchases", "Reports"), ("GET", "/history", "History tab"), ("GET", "/history/meta", "History tab: filters"),
                   ("POST", "/purchase-orders/{po_id}/receive", "Receive stock from a supplier"), ("POST", "/purchase-orders/{po_id}/cancel", "Cancel a purchase order"),
                   ("POST", "/purchase-orders/{po_id}/close", "Close a purchase order short"), ("POST", "/purchases/{rid}/pay", "Mark a supplier purchase as paid"),
                   ("POST", "/expenses/{eid}/void", "Void an expense")]:
    _p(m_, p_, "owner", None, l_)
# my own login
_p("POST", "/auth/logout", "guest", None, "Sign out")
_p("POST", "/auth/lock", "employee", None, "Lock my screen now")
_p("POST", "/auth/unlock", "employee", None, "Unlock my screen (employee: PIN, owner: password)")
_p("POST", "/auth/activity", "employee", None, "Tell the server I am still here (keeps the screen unlocked)")
_p("POST", "/auth/password", "owner", None, "Change my own password")
_p("POST", "/auth/2fa/begin", "owner", None, "Start setting up 2FA")
_p("POST", "/auth/2fa/confirm", "owner", None, "Finish setting up 2FA")
_p("POST", "/auth/2fa/disable", "owner", None, "Switch 2FA off (needs password and a code)")
# people (owner)
_p("GET", "/users", "owner", None, "Security: see people")
_p("POST", "/users", "owner", None, "Security: add an employee (PIN) or another owner (password)")
_p("POST", "/users/{uid}/update", "owner", None, "Security: change someone's name or username")
_p("POST", "/users/{uid}/password", "owner", None, "Security: reset another owner's password")
_p("POST", "/users/{uid}/pin", "owner", None, "Security: set an employee's PIN (only owners can)")
_p("POST", "/users/{uid}/active", "owner", None, "Security: deactivate / reactivate a person")
_p("POST", "/users/{uid}/remove", "owner", None, "Security: remove an employee")
_p("POST", "/users/{uid}/unlock", "owner", None, "Security: unlock a locked account or PIN")
_p("GET", "/security/policy", "owner", None, "Security: this permission table")
# settings, network, card machine (owner) and developer tools
_p("GET", "/shop", "employee", None, "Shop name, receipt footer and tax rate (for the till and receipts)")
_p("GET", "/settings", "owner", None, "Settings: see them")
_p("POST", "/settings", "owner", None, "Settings: change one")
_p("GET", "/network", "owner", None, "Network: status and the approved connection")
_p("POST", "/network/policy", "owner", None, "Network: choose the approved connection (re-asks the password)")
_p("POST", "/network/override", "developer", None, "Network: switch protection off / on (developer only)")
_p("GET", "/developer/diagnostics", "developer", None, "Developer: pipeline, monitor and settings overview")
_p("GET", "/integrations/card", "owner", None, "Card machine: is it set up (never shows the credentials)")
_p("POST", "/integrations/card/credentials", "owner", None, "Card machine: store credentials (write-only)")
_p("POST", "/integrations/card/clear", "owner", None, "Card machine: remove stored credentials")


def install(app) -> None:
    """Startup check: every route must have an explicit rule. Forgetting one stops the app from starting."""
    seen = {f"{m.upper()} {path}" for path, ops in app.openapi()["paths"].items() for m in ops}
    missing = sorted(seen - set(POLICY) - PUBLIC)
    if missing:
        raise RuntimeError("These routes have no permission rule in policy.POLICY (add them): " + ", ".join(missing))
    stale = sorted(set(POLICY) - seen)
    if stale:
        raise RuntimeError("policy.POLICY lists routes that don't exist: " + ", ".join(stale))
