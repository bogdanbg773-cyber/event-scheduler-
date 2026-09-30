from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

# Instanțe unice, importate de app.py, models.py și scheduler.py.
# Așa evităm import-uri circulare între modulele Flask.
db = SQLAlchemy()

try:
    from flask_migrate import Migrate

    migrate = Migrate()
except ImportError:  # pragma: no cover - Flask-Migrate nu e instalat încă
    migrate = None


@event.listens_for(Engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record):
    """
    SQLite NU impune FOREIGN KEY constraints implicit (e opt-in per conexiune,
    spre deosebire de PostgreSQL, unde sunt mereu impuse). Fără asta,
    ștergerea cascadată configurată pe partea Python/SQLAlchemy tot
    funcționează câtă vreme se trece prin ORM, dar baza de date însăși nu
    ar detecta/preveni un rând orfan introdus altfel (script extern, unealtă
    de administrare) - vezi punctul 21 din cerință.

    Activăm și un `busy_timeout` mic: mai multe procese care scriu aproape
    simultan (claim atomic din models.py) pot lovii ocazional
    "database is locked" pe SQLite dacă nu așteaptă puțin - PostgreSQL nu
    are nevoie de asta (folosește row-level locking nativ).

    Listener-ul verifică explicit modulul conexiunii DBAPI, deci nu are
    niciun efect pe conexiuni PostgreSQL (psycopg2) - doar pe sqlite3.
    """
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
