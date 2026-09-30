"""
Logica DECIZIONALĂ pentru retry / backoff / recovery window / orizont de
occurrence, izolată deliberat de Flask și SQLAlchemy.

De ce un modul separat:
  - Poate fi testată direct (`python -m unittest tests.test_retry_policy`)
    fără nicio dependență externă (doar stdlib: `datetime`, `math`), chiar
    și într-un mediu unde Flask-SQLAlchemy/APScheduler nu sunt instalate.
  - scheduler.py și models.py IMPORTĂ aceste funcții în loc să reimplementeze
    aceeași aritmetică în două locuri (o singură sursă de adevăr pentru
    "ce înseamnă întârziat", "ce înseamnă retry eligibil", etc).
  - Constrângerile SQL din models.py (claim_reminder / _try_reclaim) verifică
    ACELEAȘI condiții descrise aici (next_retry_at <= now, lease_until <= now)
    - documentat explicit în docstring-urile lor, ca cele două să nu ajungă
      accidental să diverge.

Nimic de aici nu atinge baza de date. Toate funcțiile sunt pure: primesc
valori, întorc valori, nu au efecte secundare.
"""

import math
from datetime import date, datetime, timedelta
from typing import Iterator, Optional


def compute_backoff_seconds(attempt_count: int, base_seconds: float, max_seconds: float) -> float:
    """
    Backoff exponențial limitat (attempt_count >= 1):
      attempt 1 -> base_seconds
      attempt 2 -> base_seconds * 2
      attempt 3 -> base_seconds * 4
      ...
    plafonat la max_seconds, ca să nu ajungem la ore/zile între încercări
    doar pentru că numărul de încercări maxime e mare.
    """
    if attempt_count < 1:
        attempt_count = 1
    backoff = base_seconds * (2 ** (attempt_count - 1))
    return min(backoff, max_seconds)


def lateness_minutes(now: datetime, scheduled_for: datetime) -> float:
    """Cât de "întârziat" e now față de scheduled_for, în minute (poate fi negativ)."""
    return (now - scheduled_for).total_seconds() / 60.0


def is_within_recovery_window(now: datetime, scheduled_for: datetime, recovery_window_minutes: float) -> bool:
    """
    True dacă reminderul mai poate fi trimis (întârzierea e în limita
    RECOVERY_WINDOW_MINUTES). False -> a devenit "missed" (prea târziu ca
    să mai aibă sens un anunț pe Discord).

    Regula (neschimbată față de etapa anterioară):
      now < scheduled_for                                   -> nu e încă due
      scheduled_for <= now <= scheduled_for + recovery_window -> se trimite
      now > scheduled_for + recovery_window                   -> missed
    """
    return lateness_minutes(now, scheduled_for) <= recovery_window_minutes


def is_retry_eligible(next_retry_at: Optional[datetime], now: datetime) -> bool:
    """True dacă un reminder 'failed' poate fi reîncercat ACUM."""
    return next_retry_at is not None and next_retry_at <= now


def is_lease_expired(lease_until: Optional[datetime], now: datetime) -> bool:
    """
    True dacă un reminder 'pending' e considerat abandonat (procesul care îl
    revendicase probabil a murit înainte să scrie rezultatul) și poate fi
    revendicat din nou de alt proces/rulare.
    """
    return lease_until is not None and lease_until <= now


def event_horizon_days(max_minutes_before: int, max_horizon_days: int) -> int:
    """
    Câte zile ÎNAINTE de azi trebuie considerate ca posibile occurrence date
    ale unui eveniment, dat fiind cel mai îndepărtat reminder activ al lui
    (minutes_before maxim), plafonat la max_horizon_days.

    Motivul (vezi punctul 9 din cerință - problema ±1 zi): un reminder cu
    minutes_before mare poate fi scadent AZI chiar dacă occurrence-ul lui e
    peste câteva zile. Nu mai presupunem "occurrence-ul relevant e mereu
    ieri/azi/mâine" - calculăm orizontul din datele reale ale evenimentului.

    max_horizon_days e o plasă de siguranță (configurabilă), ca o valoare
    aberantă introdusă din greșeală (ex. minutes_before = 999999) să nu
    facă schedulerul să scaneze mii de zile la fiecare rulare.
    """
    if not max_minutes_before or max_minutes_before <= 0:
        days = 1
    else:
        days = math.ceil(max_minutes_before / 1440) + 1
    return min(days, max_horizon_days) if max_horizon_days else days


def candidate_occurrence_dates(today: date, lookback_days: int, horizon_days: int) -> Iterator[date]:
    """
    Datele candidate (occurrence_date) pe care schedulerul trebuie să le
    verifice cu `recurrence.occurs_on(event, data)`, la o rulare care are
    loc în ziua `today`.

    `lookback_days` acoperă recuperarea după o repornire/adormire (Render
    Free) și cazul evenimentelor de lângă miezul nopții (occurrence de ieri
    cu scheduled_for tot ieri, dar procesat abia azi). `horizon_days` vine
    din `event_horizon_days` de mai sus.
    """
    if lookback_days < 0:
        lookback_days = 0
    if horizon_days < 0:
        horizon_days = 0
    current = today - timedelta(days=lookback_days)
    end = today + timedelta(days=horizon_days)
    while current <= end:
        yield current
        current += timedelta(days=1)
