import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


class Config:
    """
    Toată configurația vine din variabile de mediu.
    Nimic sensibil (webhook, secret key) nu e scris în cod.
    """

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # DATABASE_URL implicit -> SQLite local.
    # Când treci pe PostgreSQL/Supabase, setezi doar variabila DATABASE_URL
    # (ex: postgresql://user:pass@host:5432/dbname) - codul nu se schimbă.
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///events.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # "Server Time" al jocului - fus orar fix, fără conversii per-utilizator.
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "UTC")

    # Cât de des verifică scheduler-ul evenimentele (secunde).
    SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "30"))

    # Render Free poate adormi serviciul. Când aplicația (re)pornește, un
    # reminder al cărui moment ideal a trecut cu cel mult atâtea minute e
    # considerat încă recuperabil și e trimis (cu întârziere) în loc să fie
    # marcat direct "missed". Vezi scheduler.py pentru logica de recovery.
    RECOVERY_WINDOW_MINUTES = int(os.environ.get("RECOVERY_WINDOW_MINUTES", "15"))

    # Webhook-ul Discord - NICIODATĂ hardcodat.
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

    # ------------------------------------------------------------------
    # Retry / lease (etapa "reliability + data integrity" - vezi punctele
    # 4 și 5 din cerință). Valori implicite conservatoare: puține încercări,
    # backoff scurt - un panou de remindere Discord nu are nevoie de un
    # retry agresiv, doar de unul care nu renunță după primul eșec tranzitoriu.
    # ------------------------------------------------------------------

    # Câte încercări de trimitere sunt permise per (reminder, occurrence)
    # înainte ca eșecul să devină permanent ("missed"). Prima încercare
    # (claim inițial) contează ca attempt 1.
    RETRY_MAX_ATTEMPTS = int(os.environ.get("RETRY_MAX_ATTEMPTS", "5"))

    # Backoff exponențial: attempt N așteaptă
    # min(RETRY_BACKOFF_BASE_SECONDS * 2^(N-1), RETRY_BACKOFF_MAX_SECONDS)
    # înainte de a fi eligibil pentru reîncercare.
    RETRY_BACKOFF_BASE_SECONDS = float(os.environ.get("RETRY_BACKOFF_BASE_SECONDS", "20"))
    RETRY_BACKOFF_MAX_SECONDS = float(os.environ.get("RETRY_BACKOFF_MAX_SECONDS", "600"))

    # Cât timp rămâne activ un claim "pending" înainte să fie considerat
    # abandonat (procesul care l-a revendicat a murit) și recuperabil de
    # alt proces/rulare. Trebuie să fie confortabil mai mare decât timeout-ul
    # cererii HTTP către Discord (10s, vezi webhook.py) + marja de procesare.
    RETRY_LEASE_SECONDS = int(os.environ.get("RETRY_LEASE_SECONDS", "120"))

    # ------------------------------------------------------------------
    # Orizont de occurrence (etapa "reliability" - punctul 9 din cerință:
    # eliminarea dependenței rigide de ieri/azi/mâine).
    # ------------------------------------------------------------------

    # Câte zile ÎNAPOI verifică schedulerul pentru occurrence-uri posibil
    # neprocesate încă (plasă de siguranță pentru recovery după o repornire
    # mai lungă, și pentru evenimentele de lângă miezul nopții).
    REMINDER_LOOKBACK_DAYS = int(os.environ.get("REMINDER_LOOKBACK_DAYS", "1"))

    # Plafon de siguranță pentru câte zile ÎNAINTE verifică schedulerul,
    # indiferent cât de mare ar fi minutes_before al unui reminder - vezi
    # retry_policy.event_horizon_days(). Implicit generos (nu avem încă
    # recurențe săptămânale/lunare implementate, deci reminderele reale nu
    # ar trebui să aibă nevoie de un orizont mai mare de câteva zile).
    MAX_REMINDER_HORIZON_DAYS = int(os.environ.get("MAX_REMINDER_HORIZON_DAYS", "7"))

    # Limita de intrare trebuie să fie coerentă cu plafonul de occurrence.
    # Important: nu permitem o valoare validă în formular care apoi ar fi
    # ignorată în tăcere de scheduler din cauza MAX_REMINDER_HORIZON_DAYS.
    MAX_REMINDER_MINUTES_BEFORE = int(
        os.environ.get("MAX_REMINDER_MINUTES_BEFORE", str(MAX_REMINDER_HORIZON_DAYS * 1440))
    )

    # Dezvoltare/bootstrap: dacă "true" (implicit), create_app() apelează
    # db.create_all() pentru tabelele care lipsesc încă - util pe o bază de
    # date NOUĂ, goală (SQLite local nou, ex. la clonarea proiectului).
    # NU e un substitut pentru migrații (vezi punctul 20 din cerință și
    # README, secțiunea "Migrații") - pe o bază de date care are deja
    # schema veche (dinaintea acestei etape), db.create_all() NU adaugă
    # coloanele noi pe tabele EXISTENTE; e nevoie de `flask db upgrade`.
    DB_AUTO_CREATE = os.environ.get("DB_AUTO_CREATE", "true").lower() == "true"


def validate_scheduler_limits(max_horizon_days: int, max_minutes_before: int) -> None:
    """Refuză o configurație în care formularul acceptă remindere pe care
    schedulerul nu le poate căuta în orizontul configurat."""
    if max_horizon_days < 0:
        raise RuntimeError("MAX_REMINDER_HORIZON_DAYS nu poate fi negativ.")
    if max_minutes_before < 0:
        raise RuntimeError("MAX_REMINDER_MINUTES_BEFORE nu poate fi negativ.")
    allowed = max_horizon_days * 1440
    if max_minutes_before > allowed:
        raise RuntimeError(
            "MAX_REMINDER_MINUTES_BEFORE depășește "
            "MAX_REMINDER_HORIZON_DAYS * 1440; configurația ar permite "
            "remindere pe care schedulerul nu le poate găsi."
        )


def validate_app_timezone(tz_name: str) -> None:
    """
    Validează explicit APP_TIMEZONE la pornirea aplicației.

    Înainte, un APP_TIMEZONE invalid cădea silențios pe UTC în scheduler
    (comportament periculos: administratorul ar crede că reminder-ele merg
    pe fusul orar corect, când de fapt merg pe UTC, fără niciun avertisment
    vizibil). Acum: dacă valoarea nu e un fus orar IANA valid, logăm o
    eroare clară și oprim pornirea aplicației (fail fast), ca problema să
    fie vizibilă imediat, nu descoperită abia când reminder-ele ajung la
    ora greșită.
    """
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        logger.error(
            "APP_TIMEZONE=%r nu este un fus orar IANA valid. "
            "Aplicația NU pornește cu un fus orar invalid tratat silențios ca UTC. "
            "Setează o valoare validă (ex: 'UTC', 'Europe/Bucharest').",
            tz_name,
        )
        raise RuntimeError(f"APP_TIMEZONE invalid: {tz_name!r}") from exc
