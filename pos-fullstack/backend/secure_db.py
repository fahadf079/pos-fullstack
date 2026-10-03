"""
Lock the database down so the POS itself can no longer tamper with its own history.   (run ONCE)

    python secure_db.py --admin-url postgresql://postgres:YOUR_POSTGRES_PASSWORD@localhost:5432/pos

What it does
  1. creates (or re-passwords) a restricted login called  pos_app  with a long random password
  2. makes sure all tables are up to date, then gives pos_app ONLY what the POS needs:
        - add / change rows in the normal tables           (no deleting sales, products, users ...)
        - ADD-ONLY on  audit_log, movements, product_log   (no edit, no delete: enforced by permissions as well as by the triggers)
        - NO changing table structure, NO dropping, NO switching the append-only triggers off
  3. writes the new  DATABASE_URL  (the pos_app login) into backend/.env  (the old file is kept as .env.bak)
  4. proves it: connects as pos_app and tries to do the forbidden things

--admin-url must be a login that is allowed to create logins: the "postgres" administrator you set when you installed
PostgreSQL. (The table work is still done as the database's own owner, "pos".)

After this the POS runs as pos_app. Keep the postgres / owner passwords somewhere safe (NOT in .env): you need them only
to upgrade the database structure (python init_db.py --admin-url ...), to restore a backup, and for migrations.
"""
import argparse
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent))
import database  # noqa: E402


def app_url_for(admin_url: str, role: str, password: str) -> str:
    u = urlparse(admin_url)
    return f"postgresql://{quote(role)}:{quote(password, safe='')}@{u.hostname or 'localhost'}:{u.port or 5432}{u.path}"


def harden(admin_url: str, role: str = database.APP_ROLE, password: str | None = None) -> str:
    """Creates/updates the restricted login and its permissions. Returns the pos_app connection URL."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", role):
        raise SystemExit("Role name must be lower-case letters, digits and underscores.")
    password = password or secrets.token_urlsafe(24)
    dbname = urlparse(admin_url).path.lstrip("/")
    with psycopg.connect(admin_url, autocommit=True) as c:
        exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
        c.execute(sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
            sql.SQL("ALTER" if exists else "CREATE"), sql.Identifier(role), sql.Literal(password)))
        c.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), sql.Identifier(role)))
        try:
            c.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        except psycopg.errors.InsufficientPrivilege:
            pass                                                # not the schema's owner: fine, newer PostgreSQL already does this
    os.environ["POS_APP_ROLE"] = role
    database.APP_ROLE = role
    database.init(admin_url)                    # tables up to date + permissions for the role
    return app_url_for(admin_url, role, password)


FORBIDDEN = [
    ("edit the History", "UPDATE audit_log SET summary='x'"),
    ("delete from the History", "DELETE FROM audit_log"),
    ("edit the stock ledger", "UPDATE movements SET delta=0"),
    ("delete a sale", "DELETE FROM sales"),
    ("delete a person", "DELETE FROM users"),
    ("delete a product", "DELETE FROM products"),
    ("switch the append-only guards off", "ALTER TABLE audit_log DISABLE TRIGGER ALL"),
    ("drop a table", "DROP TABLE sales"),
    ("change a table", "ALTER TABLE products ADD COLUMN hacked INT"),
    ("empty a table", "TRUNCATE sales"),
]


def prove(app_url: str) -> list[str]:
    """Connects as the restricted login and checks it can work but can't tamper. Returns problems (empty = good)."""
    problems = []
    with psycopg.connect(app_url) as c:
        try:
            c.execute("SELECT COUNT(*) FROM products").fetchone(); c.execute("SELECT COUNT(*) FROM audit_log").fetchone()
        except Exception as e:
            problems.append(f"pos_app can't read the tables it needs: {e}")
        c.rollback()
        for what, q in FORBIDDEN:
            try:
                c.execute(q)
                problems.append(f"pos_app was able to {what}!")
            except psycopg.errors.InsufficientPrivilege:
                pass
            except Exception as e:
                problems.append(f"unexpected result when testing '{what}': {e}")
            c.rollback()
    return problems


def write_env(app_url: str) -> Path:
    env = Path(__file__).with_name(".env")
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    if env.exists():
        Path(str(env) + ".bak").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out, done = [], False
    for ln in lines:
        if ln.strip().startswith("DATABASE_URL="):
            out.append(f"DATABASE_URL={app_url}"); done = True
        elif ln.strip().startswith("DATABASE_ADMIN_URL="):
            continue                                            # the owner login must not live in .env
        else:
            out.append(ln)
    if not done:
        out.insert(0, f"DATABASE_URL={app_url}")
    env.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--admin-url", default=os.environ.get("DATABASE_ADMIN_URL"), help="the postgres administrator, e.g. postgresql://postgres:PASSWORD@localhost:5432/pos")
    ap.add_argument("--role", default=database.APP_ROLE)
    ap.add_argument("--app-password", help="password for pos_app (default: a long random one)")
    ap.add_argument("--no-env", action="store_true", help="don't touch backend/.env, just print the new DATABASE_URL")
    a = ap.parse_args()
    if not a.admin_url:
        print("Give the postgres administrator login:  python secure_db.py --admin-url postgresql://postgres:YOUR_POSTGRES_PASSWORD@localhost:5432/pos"); return 2
    try:
        url = harden(a.admin_url, a.role, a.app_password)
    except psycopg.OperationalError as e:
        print(f"Can't connect with that login: {e}"); return 1
    problems = prove(url)
    if problems:
        print("PROBLEM: the lock-down did not work completely:\n  - " + "\n  - ".join(problems)); return 1
    print(f"OK: login '{a.role}' created and locked down. Verified: it can run the POS, but cannot edit/delete history, delete sales/people/products, change or drop tables, or switch the guards off.")
    if a.no_env:
        print(f"\nPut this in backend/.env:\nDATABASE_URL={url}")
    else:
        print(f"Wrote DATABASE_URL for '{a.role}' to {write_env(url)}  (old file kept as .env.bak). Restart the backend.")
    print("Keep the postgres and owner (pos) passwords safe: they are needed only for init_db.py (upgrades), restoring backups and migrations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
