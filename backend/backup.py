"""
Automatic backup of the POS database.

    python backend/backup.py              # writes backups/pos-YYYYMMDD-HHMMSS.dump, checks it, keeps the newest 14
    python backend/backup.py --keep 30 --dir D:\\pos-backups

Run it every day with schedule-backup.bat (Windows Task Scheduler). The dump is a complete copy of the
database (every table, the History, the ledger). Each file is checked after writing (it must be readable by
pg_restore), and written under a temporary name first, so a crash never leaves a half-written "good" backup.
Needs `pg_dump` (installed with PostgreSQL) on PATH, or Docker with the bundled docker-compose.yml.

Restore (into an empty database):   pg_restore --no-owner -d pos backups\\pos-20261002-020000.dump
"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from database import DATABASE_URL  # noqa: E402  (also loads backend/.env)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent / "backups"))
    ap.add_argument("--keep", type=int, default=int(os.environ.get("POS_BACKUP_KEEP", "14")), help="how many backups to keep (default 14)")
    a = ap.parse_args()
    out_dir = Path(a.dir); out_dir.mkdir(parents=True, exist_ok=True)
    u = urlparse(DATABASE_URL); dbname = u.path.lstrip("/"); user = unquote(u.username or "postgres")
    env = {**os.environ, "PGPASSWORD": unquote(u.password or "")}
    final = out_dir / f"pos-{datetime.now():%Y%m%d-%H%M%S}.dump"; tmp = final.with_suffix(".part")

    try:
        if shutil.which("pg_dump"):
            cmd = ["pg_dump", "-Fc", "-h", u.hostname or "localhost", "-p", str(u.port or 5432), "-U", user, "-f", str(tmp), dbname]
            subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
        elif shutil.which("docker"):      # PostgreSQL running from the bundled docker-compose.yml
            root = Path(__file__).resolve().parent.parent
            with open(tmp, "wb") as f:
                subprocess.run(["docker", "compose", "exec", "-T", "db", "pg_dump", "-Fc", "-U", user, dbname], cwd=root, check=True, stdout=f, stderr=subprocess.PIPE)
        else:
            print("BACKUP FAILED: pg_dump not found. Install PostgreSQL's command-line tools or use Docker."); return 2
        if not tmp.exists() or tmp.stat().st_size < 1024:
            raise RuntimeError("backup file is empty")
        if shutil.which("pg_restore"):    # prove the file is a readable dump
            subprocess.run(["pg_restore", "--list", str(tmp)], check=True, capture_output=True)
        tmp.replace(final)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        detail = getattr(e, "stderr", "") or ""
        print(f"BACKUP FAILED: {e} {detail.strip() if isinstance(detail, str) else detail.decode(errors='replace').strip()}"); return 1

    old = sorted(out_dir.glob("pos-*.dump"))[:-max(1, a.keep)]
    for f in old: f.unlink()
    print(f"Backup OK: {final} ({final.stat().st_size / 1024:.0f} KB). Kept {min(len(list(out_dir.glob('pos-*.dump'))), a.keep)} backup(s), removed {len(old)} old.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
