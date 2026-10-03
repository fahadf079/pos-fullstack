"""
Create / upgrade the database tables using the OWNER login (needed only after an update that changes the tables,
when the POS itself runs as the restricted pos_app login).

    python init_db.py --admin-url postgresql://pos:YOUR_OWNER_PASSWORD@localhost:5432/pos

Safe to run any time: it only adds what is missing, and re-applies pos_app's permissions.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import database  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--admin-url", default=os.environ.get("DATABASE_ADMIN_URL"))
    a = ap.parse_args()
    if not a.admin_url:
        print("Give the OWNER login:  python init_db.py --admin-url postgresql://pos:YOUR_OWNER_PASSWORD@localhost:5432/pos"); return 2
    database.init(a.admin_url)
    print("Database structure is up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
