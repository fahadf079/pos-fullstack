"""
database.py — the PostgreSQL layer for the POS (replaces the old shared SQLite connection).

How it works
  * A connection pool (psycopg 3). Every write endpoint runs inside ONE real transaction (`@tx` /
    `transaction()`): it commits when the endpoint succeeds and rolls EVERYTHING back on any error.
  * Rows that must not be changed concurrently (product stock, a cart line, a sale being refunded,
    a PO being received) are locked with SELECT … FOR UPDATE, so two tills can never oversell the
    last unit or double-receive a delivery. A deadlock/serialization failure is retried automatically.
  * `db.execute("... ? ...", params)` keeps the old SQLite-style `?` placeholders, so the endpoint code
    reads the same as before. Inside a transaction it uses that transaction's connection.
  * The live-update counter is bumped only AFTER the commit (screens must never refetch half-done data).
  * `audit()` writes the append-only History (audit_log) in the same transaction as the change itself.

Config (environment variables, or backend/.env):  DATABASE_URL, POS_TZ (default Asia/Karachi), POS_POOL_MAX,
  DATABASE_ADMIN_URL (optional: owner login used ONLY to create/upgrade tables), POS_APP_ROLE (default pos_app).
"""
import contextvars
import functools
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.adapt import Loader
from psycopg.types.datetime import TimestamptzLoader
from psycopg_pool import ConnectionPool


def _load_env_file() -> None:
    f = Path(__file__).with_name(".env")
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos")
TZNAME = os.environ.get("POS_TZ", "Asia/Karachi")
TZ = ZoneInfo(TZNAME)
ADMIN_URL = os.environ.get("DATABASE_ADMIN_URL") or None     # owner login, used only for schema changes
APP_ROLE = os.environ.get("POS_APP_ROLE", "pos_app")
assert re.fullmatch(r"[a-z_][a-z0-9_]*", APP_ROLE), "POS_APP_ROLE must be a plain lower-case role name"


# ───────────── type handling: NUMERIC -> float, timestamptz -> local ISO string ─────────────
class _NumLoader(Loader):
    def load(self, data):
        return float(bytes(data))


class _IsoTs(TimestamptzLoader):
    def load(self, data):
        return super().load(data).astimezone(TZ).isoformat(timespec="seconds")


psycopg.adapters.register_loader("numeric", _NumLoader)
psycopg.adapters.register_loader("timestamptz", _IsoTs)


class Row(tuple):
    """A result row usable both as row[0] and row["name"] (and dict(row))."""
    def keys(self):
        return self._names

    def __getitem__(self, k):
        return tuple.__getitem__(self, self._idx[k] if isinstance(k, str) else k)


def _row_factory(cursor):
    names = tuple(c.name for c in (cursor.description or ()))
    idx = {n: i for i, n in enumerate(names)}

    def make(values):
        r = Row(values)
        r._names, r._idx = names, idx
        return r
    return make


class Result:
    def __init__(self, rows, rowcount, lastrowid=None):
        self.rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


# tables whose primary key is a serial `id` — INSERTs into them return it as .lastrowid
_ID_TABLES = {"products", "movements", "sales", "refunds", "product_log", "suppliers", "purchase_orders",
              "po_items", "receipts", "receipt_items", "expenses", "audit_log", "users"}
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


def _sql(sql: str, params):
    if params is None:
        return sql
    return sql.replace("%", "%%").replace("?", "%s")


# ───────────── live-update counter ─────────────
_version = 0


def get_version() -> int:
    return _version


def _bump_now() -> None:
    global _version
    _version += 1


# ───────────── pool / transactions ─────────────
_pool: ConnectionPool | None = None
_state: contextvars.ContextVar = contextvars.ContextVar("pos_tx", default=None)


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=int(os.environ.get("POS_POOL_MAX", "20")),
                               kwargs={"row_factory": _row_factory, "options": f"-c timezone={TZNAME}"}, open=False)
        try:
            _pool.open(wait=True, timeout=10)
        except Exception as e:
            _pool = None
            raise RuntimeError(f"Cannot reach PostgreSQL at {DATABASE_URL.split('@')[-1]} — is it running? ({e})") from None
    return _pool


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _run(conn, sql, params, many=False):
    if many:
        cur = conn.cursor()
        cur.executemany(_sql(sql, params), params)    # `params` is a list of rows (never None here)
        return Result([], cur.rowcount)
    m = _INSERT.match(sql)
    want_id = bool(m and m.group(1).lower() in _ID_TABLES and "returning" not in sql.lower())
    cur = conn.execute(_sql(sql + (" RETURNING id" if want_id else ""), params), params)
    rows = cur.fetchall() if cur.description else []
    return Result(rows, cur.rowcount, rows[0][0] if (want_id and rows) else None)


class _DB:
    """Stand-in for the old sqlite connection: same execute()/executemany() calls, now Postgres."""

    def execute(self, sql, params=None):
        st = _state.get()
        if st:
            return _run(st["conn"], sql, params)
        with _get_pool().connection() as conn:
            return _run(conn, sql, params)

    def executemany(self, sql, seq):
        seq = [tuple(x) for x in seq]
        st = _state.get()
        if st:
            return _run(st["conn"], sql, seq, many=True)
        with _get_pool().connection() as conn:
            return _run(conn, sql, seq, many=True)

    def commit(self):   # kept so old call sites still read fine: the transaction commits when the endpoint ends
        pass

    def rollback(self):
        pass


db = _DB()


@contextmanager
def transaction(snapshot: bool = False):
    """One real transaction. Joins the surrounding one if there is one. snapshot=True is a read-only,
    consistent view (used by reports so their numbers agree with each other)."""
    if _state.get():
        yield db
        return
    st = {"bump": False}
    with _get_pool().connection() as conn:       # commits on success, rolls back on any exception
        st["conn"] = conn
        tok = _state.set(st)
        try:
            if snapshot:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield db
        finally:
            _state.reset(tok)
    if st["bump"]:
        _bump_now()


@contextmanager
def independent():
    """A transaction on its OWN connection that commits even if the surrounding request later fails.
    Used for things that must be remembered no matter what: failed logins, wrong PINs, lockouts."""
    tok = _state.set(None)
    try:
        with transaction() as c:
            yield c
    finally:
        _state.reset(tok)


# ───────────── who is acting (set by auth.py for every logged-in request) ─────────────
_actor: contextvars.ContextVar = contextvars.ContextVar("pos_actor", default=None)


def set_actor(user_id, username: str):
    """Remember who the current request belongs to. audit() and the stock ledger read it automatically."""
    return _actor.set({"id": user_id, "username": username, "approved_by": None})


def actor_pair():
    a = _actor.get()
    return (a["id"], a["username"]) if a else (None, "system")


def set_approver(username: str) -> None:
    a = _actor.get()
    if a is not None:
        a["approved_by"] = username


def bump() -> None:
    """Tell open screens something changed. Inside a transaction this waits for the commit."""
    st = _state.get()
    if st:
        st["bump"] = True
    else:
        _bump_now()


def tx(fn):
    """Run an endpoint in one transaction; retry if Postgres picked it as a deadlock victim."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        for attempt in range(5):
            try:
                with transaction():
                    return fn(*a, **kw)
            except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
                if attempt == 4:
                    raise
                time.sleep(0.03 * (attempt + 1))
    return wrapper


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def jl(x):
    """JSONB comes back already parsed; text/legacy comes back as a string."""
    return __import__("json").loads(x) if isinstance(x, (str, bytes)) else x


def audit(action: str, entity: str = "", entity_id="", summary: str = "", details=None,
          actor: str | None = None, user_id: int | None = None) -> None:
    """Append one line to the History. Runs in the caller's transaction, so a change and its history
    row are saved together or not at all. WHO did it comes from the logged-in user of this request
    (and `approved_by` when a manager's PIN approved a cashier's action)."""
    import json
    a = _actor.get() or {}
    db.execute("INSERT INTO audit_log(ts, user_id, actor, action, entity, entity_id, summary, details, approved_by) VALUES (?,?,?,?,?,?,?,?,?)",
               (now(), user_id if user_id is not None else (None if actor else a.get("id")), actor or a.get("username") or "system",
                action, entity, str(entity_id), summary, None if details is None else json.dumps(details, default=str),
                a.get("approved_by") if not actor else None))


# ───────────── schema ─────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  id BIGSERIAL PRIMARY KEY,
  sku TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  cat TEXT NOT NULL DEFAULT 'Grocery',
  price NUMERIC(14,2) NOT NULL DEFAULT 0,
  stock NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK (stock >= 0),
  cost NUMERIC(14,2) NOT NULL DEFAULT 0,
  unit TEXT NOT NULL DEFAULT 'pc',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cart(
  product_id BIGINT PRIMARY KEY REFERENCES products(id),
  qty NUMERIC(14,3) NOT NULL CHECK (qty > 0)
);
CREATE TABLE IF NOT EXISTS movements(
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  delta NUMERIC(14,3) NOT NULL,
  before NUMERIC(14,3) NOT NULL,
  after NUMERIC(14,3) NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_movements_sku ON movements(sku);
CREATE TABLE IF NOT EXISTS sales(
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  subtotal NUMERIC(14,2) NOT NULL DEFAULT 0,
  discount NUMERIC(14,2) NOT NULL DEFAULT 0,
  tax NUMERIC(14,2) NOT NULL DEFAULT 0,
  total NUMERIC(14,2) NOT NULL,
  discount_code TEXT,
  payment TEXT,
  items JSONB NOT NULL,
  refunded INTEGER NOT NULL DEFAULT 0,
  refund_note TEXT,
  refund_ts TIMESTAMPTZ,
  refunded_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  refunded_net NUMERIC(14,2) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sales_ts ON sales(ts);
CREATE TABLE IF NOT EXISTS refunds(
  id BIGSERIAL PRIMARY KEY,
  sale_id BIGINT NOT NULL REFERENCES sales(id),
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  note TEXT NOT NULL DEFAULT '',
  amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  net NUMERIC(14,2) NOT NULL DEFAULT 0,
  items JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_refunds_sale ON refunds(sale_id);
CREATE TABLE IF NOT EXISTS product_log(
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  product_id BIGINT NOT NULL REFERENCES products(id),
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  field TEXT NOT NULL,
  old TEXT,
  new TEXT
);
CREATE INDEX IF NOT EXISTS ix_plog_product ON product_log(product_id);
CREATE TABLE IF NOT EXISTS suppliers(
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  phone TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_suppliers_name ON suppliers(lower(name));
CREATE TABLE IF NOT EXISTS purchase_orders(
  id BIGSERIAL PRIMARY KEY,
  supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  expected TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ORDERED',
  note TEXT NOT NULL DEFAULT '',
  closed_ts TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_po_status ON purchase_orders(status);
CREATE TABLE IF NOT EXISTS po_items(
  id BIGSERIAL PRIMARY KEY,
  po_id BIGINT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
  product_id BIGINT REFERENCES products(id),
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  cat TEXT NOT NULL DEFAULT 'Grocery',
  sale_price NUMERIC(14,2) NOT NULL DEFAULT 0,
  unit TEXT NOT NULL DEFAULT 'pc',
  ordered NUMERIC(14,3) NOT NULL CHECK (ordered > 0),
  received NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK (received >= 0),
  unit_cost NUMERIC(14,4) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_poitems_po ON po_items(po_id);
CREATE TABLE IF NOT EXISTS receipts(
  id BIGSERIAL PRIMARY KEY,
  po_id BIGINT NOT NULL REFERENCES purchase_orders(id),
  supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  total NUMERIC(14,2) NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  paid INTEGER NOT NULL DEFAULT 0,
  paid_ts TIMESTAMPTZ,
  pay_method TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_receipts_ts ON receipts(ts);
CREATE TABLE IF NOT EXISTS receipt_items(
  id BIGSERIAL PRIMARY KEY,
  receipt_id BIGINT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  po_item_id BIGINT NOT NULL REFERENCES po_items(id),
  product_id BIGINT NOT NULL REFERENCES products(id),
  name TEXT NOT NULL,
  qty NUMERIC(14,3) NOT NULL CHECK (qty > 0),
  unit_cost NUMERIC(14,4) NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses(
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  category TEXT NOT NULL,
  amount NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  payee TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  voided INTEGER NOT NULL DEFAULT 0,
  voided_ts TIMESTAMPTZ,
  void_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_expenses_ts ON expenses(ts);
CREATE TABLE IF NOT EXISTS audit_log(
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_id INTEGER,
  actor TEXT NOT NULL DEFAULT 'system',
  action TEXT NOT NULL,
  entity TEXT NOT NULL DEFAULT '',
  entity_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  details JSONB
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action);

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS approved_by TEXT;
ALTER TABLE movements ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE movements ADD COLUMN IF NOT EXISTS actor TEXT NOT NULL DEFAULT 'system';
ALTER TABLE sales ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS actor TEXT NOT NULL DEFAULT 'system';
CREATE TABLE IF NOT EXISTS users(
  id BIGSERIAL PRIMARY KEY,
  username TEXT NOT NULL,
  full_name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL CHECK (role IN ('owner','manager','cashier')),
  pw_hash TEXT NOT NULL,
  pin_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  must_change INTEGER NOT NULL DEFAULT 0,
  failed_logins INTEGER NOT NULL DEFAULT 0,
  locked_until TIMESTAMPTZ,
  pin_failed INTEGER NOT NULL DEFAULT 0,
  pin_locked_until TIMESTAMPTZ,
  created_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_ts TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_name ON users(lower(username));
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  created_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_ts TIMESTAMPTZ NOT NULL,
  last_seen_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  ip TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

CREATE OR REPLACE FUNCTION pos_append_only() RETURNS trigger AS $fn$
BEGIN
  RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP;
END;
$fn$ LANGUAGE plpgsql;
"""

# history-style tables the app must never edit or delete from
APPEND_ONLY = ("audit_log", "movements", "product_log")


# What the app's own (restricted) database login may do. It can NOT edit or delete history, delete sales,
# change table structure, drop anything or switch off the append-only triggers: only the owner can.
_RW = ("products", "sales", "refunds", "suppliers", "purchase_orders", "po_items", "receipts", "receipt_items", "expenses", "users")
_FULL = ("cart", "sessions")
_APPEND = ("movements", "audit_log", "product_log")


def _grant_app_role(c) -> None:
    if not c.execute(f"SELECT 1 FROM pg_roles WHERE rolname='{APP_ROLE}'").fetchone():
        return
    c.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    c.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    c.execute(f"GRANT SELECT, INSERT, UPDATE ON {', '.join(_RW)} TO {APP_ROLE}")
    c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(_FULL)} TO {APP_ROLE}")
    c.execute(f"GRANT SELECT, INSERT ON {', '.join(_APPEND)} TO {APP_ROLE}")
    c.execute(f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")


def _ddl(c) -> None:
    c.execute("SELECT pg_advisory_xact_lock(727001)")           # two app processes starting together
    for stmt in [s.strip() for s in re.split(r";\s*\n(?=CREATE|ALTER|DROP)", SCHEMA) if s.strip()]:
        c.execute(stmt.rstrip(";"))
    for t in APPEND_ONLY:
        c.execute(f"DROP TRIGGER IF EXISTS {t}_no_edit ON {t}")
        c.execute(f"CREATE TRIGGER {t}_no_edit BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION pos_append_only()")
        c.execute(f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t}")
        c.execute(f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION pos_append_only()")
    _grant_app_role(c)


def _schema_current() -> bool:
    r = db.execute("SELECT (SELECT count(*) FROM information_schema.columns WHERE table_name='audit_log' AND column_name='approved_by')"
                   " + (SELECT count(*) FROM information_schema.columns WHERE table_name='movements' AND column_name='actor')"
                   " + (SELECT count(*) FROM information_schema.tables WHERE table_name IN ('users','sessions'))").fetchone()
    return r[0] == 4


def init(admin_url: str | None = None) -> None:
    """Connect, create/upgrade the schema, install the append-only guards. Safe to run on every start.

    With DATABASE_ADMIN_URL (or admin_url) the structure work is done by that owner login. Without it the
    app's own login is used; if that login is the restricted `pos_app` role it can't change tables, which
    is fine as long as the schema is already current (otherwise: run init_db.py once as the owner)."""
    url = admin_url or ADMIN_URL
    if url:
        with psycopg.connect(url, options=f"-c timezone={TZNAME}") as conn:      # commits on success
            # If the login is the postgres superuser, still do the table work AS THE DATABASE'S OWNER (so the tables are
            # always owned by one login and a later upgrade by that owner just works).
            owner = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()").fetchone()[0]
            if owner != conn.execute("SELECT current_user").fetchone()[0]:
                conn.execute(f'SET ROLE "{owner}"')
            _ddl(conn)
        return
    try:
        with transaction() as c:
            _ddl(c)
    except psycopg.errors.InsufficientPrivilege:
        if not _schema_current():
            raise RuntimeError("The database structure needs an upgrade, but the app's login is not allowed to change it. "
                               "Run once, as the database owner:  python init_db.py --admin-url postgresql://OWNER:PASSWORD@localhost:5432/pos") from None
