"""
Validare și parsare a formularului de eveniment, izolate deliberat de
Flask/SQLAlchemy (depind doar de `re`, `datetime` și `recurrence` - acesta
din urmă la rândul lui fără dependențe externe). Așa pot fi testate direct
(`tests/test_forms.py`) chiar și într-un mediu unde Flask-SQLAlchemy nu e
instalat.

app.py importă aceste funcții în loc să le redefinească inline.
"""

import re
from datetime import date, datetime

from recurrence import REGISTRY as RECURRENCE_REGISTRY

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Formularul actual (versiune "de tranziție", nu interfața finală - vezi
# README) cere un număr FIX de sloturi de reminder, ca să rămână un simplu
# formular HTML server-rendered, fără JavaScript de adăugat/șters rânduri
# dinamic. Modelul de date (EventReminder) NU are nicio limită - un
# eveniment poate avea oricâte remindere prin API/DB. Limita de mai jos e
# doar o constrângere temporară a acestui formular minimal (punctul 17 din
# cerință).
MAX_REMINDER_SLOTS = 5
MAX_DISCORD_MESSAGE_LENGTH = 2000
MAX_REMINDER_MINUTES_BEFORE = 7 * 24 * 60


def parse_start_date(raw: str):
    """Parsează data de referință din formular. Implicit azi dacă lipsește."""
    raw = (raw or "").strip()
    if not raw:
        return date.today(), None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, "Data de început trebuie să fie o dată validă (AAAA-LL-ZZ)."


def parse_reminder_slots(form, max_minutes_before=MAX_REMINDER_MINUTES_BEFORE):
    """
    Citește sloturile de reminder din formular:
      reminder_id_1..N       - ID-ul EventReminder existent (gol pt. un slot nou)
      reminder_delete_1..N   - checkbox "șterge acest reminder" (doar pt. sloturi cu id)
      reminder_minutes_1..N / reminder_message_1..N - conținutul reminderului

    Returnează (reminders: list[dict], errors: list[str]). Fiecare dict are
    cheile: id (int|None), delete (bool), minutes_before (int|None),
    message (str|None).

    IMPORTANT (punctul 3 din cerință - "editarea NU trebuie să se comporte
    ca ștergere+recreare"): un slot care corespunde unui reminder EXISTENT
    (are id) nu poate fi lăsat gol "din greșeală" - dacă utilizatorul vrea
    să elimine reminderul, trebuie să bifeze explicit "Șterge acest
    reminder". Un slot gol FĂRĂ id e pur și simplu neutilizat (nu e o
    eroare) - la fel ca înainte.
    """
    reminders = []
    errors = []
    any_used = False

    for i in range(1, MAX_REMINDER_SLOTS + 1):
        raw_id = (form.get(f"reminder_id_{i}") or "").strip()
        reminder_id = int(raw_id) if raw_id.isdigit() else None
        delete_flag = bool(form.get(f"reminder_delete_{i}"))
        raw_minutes = (form.get(f"reminder_minutes_{i}") or "").strip()
        raw_message = (form.get(f"reminder_message_{i}") or "").strip()

        if delete_flag:
            if reminder_id is None:
                errors.append(f"Reminderul #{i}: nu poate fi bifat pentru ștergere (nu are un ID existent).")
                continue
            any_used = True
            reminders.append(
                {"id": reminder_id, "delete": True, "minutes_before": None, "message": None}
            )
            continue

        if raw_minutes == "" and raw_message == "":
            if reminder_id is not None:
                errors.append(
                    f"Reminderul #{i}: nu poate fi lăsat gol. Bifează „Șterge acest "
                    f"reminder” dacă vrei să îl elimini, altfel completează-l la loc."
                )
            continue  # slot neutilizat (nou, neatins de utilizator)

        any_used = True

        if raw_minutes == "":
            errors.append(f"Reminderul #{i}: lipsesc minutele înainte.")
            continue

        try:
            minutes_before = int(raw_minutes)
        except ValueError:
            errors.append(f"Reminderul #{i}: minutele trebuie să fie un număr întreg.")
            continue

        if minutes_before < 0:
            errors.append(f"Reminderul #{i}: minutele nu pot fi negative.")
            continue

        if not raw_message:
            errors.append(f"Reminderul #{i}: mesajul Discord este obligatoriu.")
            continue

        if len(raw_message) > MAX_DISCORD_MESSAGE_LENGTH:
            errors.append(
                f"Reminderul #{i}: mesajul Discord nu poate depăși "
                f"{MAX_DISCORD_MESSAGE_LENGTH} de caractere."
            )
            continue

        if max_minutes_before is not None and minutes_before > max_minutes_before:
            errors.append(
                f"Reminderul #{i}: nu poate fi setat cu mai mult de "
                f"{max_minutes_before} minute înainte. "
                f"Dacă ai nevoie de un interval mai mare, mărește "
                f"MAX_REMINDER_MINUTES_BEFORE din configurație."
            )
            continue

        reminders.append(
            {"id": reminder_id, "delete": False, "minutes_before": minutes_before, "message": raw_message}
        )

    if not any_used and not errors:
        errors.append("Evenimentul are nevoie de cel puțin un reminder.")

    return reminders, errors


def validate_event_form(form):
    """
    Validează datele din formularul de eveniment (partea "de bază" a
    evenimentului - numele, ora, recurența, data de start). Reminderele
    se validează separat cu parse_reminder_slots.

    Nu lăsăm nicio conversie (int(), datetime.strptime() etc.) să arunce o
    excepție necontrolată (500) dacă utilizatorul introduce ceva invalid -
    totul devine un mesaj de eroare afișat pe formular.
    """
    errors = []

    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Numele evenimentului este obligatoriu.")

    event_time = (form.get("event_time") or "").strip()
    if not event_time or not _TIME_RE.match(event_time):
        errors.append("Ora evenimentului trebuie să fie în format HH:MM (24h), ex: 20:30.")

    start_date, start_date_error = parse_start_date(form.get("start_date"))
    if start_date_error:
        errors.append(start_date_error)

    recurrence_type = (form.get("recurrence_type") or "").strip()
    if recurrence_type not in RECURRENCE_REGISTRY:
        errors.append("Tipul de recurență selectat nu este valid.")

    recurrence_interval_days = None
    if recurrence_type == "every_x_days":
        raw_interval = (form.get("recurrence_interval_days") or "").strip()
        try:
            recurrence_interval_days = int(raw_interval)
            if recurrence_interval_days < 1:
                errors.append("Intervalul (zile) trebuie să fie cel puțin 1.")
        except ValueError:
            errors.append("Intervalul în zile este obligatoriu și trebuie să fie un număr întreg.")

    active = bool(form.get("active"))

    cleaned = {
        "name": name,
        "event_time": event_time,
        "start_date": start_date or date.today(),
        "recurrence_type": recurrence_type or "daily",
        "recurrence_interval_days": recurrence_interval_days,
        "active": active,
    }
    return cleaned, errors
