"""
Motorul de recurență.

Un singur job: pentru un Event și o dată calendaristică dată, răspunde la
întrebarea "are loc evenimentul în ziua asta?" (occurs_on).

Arhitectură extensibilă în mod deliberat: fiecare tip de recurență e o
funcție simplă (event, date) -> bool, înregistrată în REGISTRY sub un nume
("daily", "every_x_days", ...). Pentru a adăuga un tip nou de recurență în
viitor (weekly, zile specifice ale săptămânii, lunar, date specifice etc.)
e nevoie doar de:

  1. o funcție nouă aici, cu aceeași semnătură;
  2. o intrare nouă în REGISTRY;
  3. eventual, coloane noi pe Event pentru parametrii specifici tipului
     (ex: "days_of_week" pentru recurența săptămânală).

Nimic din scheduler.py sau din logica de claim/occurrence nu trebuie
schimbat când se adaugă un tip nou - de asta motorul e separat de restul.

Implementate ACUM (suficient pentru cazurile reale ale clanului, fără
supra-inginerie):
  - "daily"        -> zilnic, începând cu event.start_date.
  - "every_x_days"  -> la fiecare N zile, folosind event.start_date ca
                       referință (N = event.recurrence_interval_days).

NEimplementate încă (arhitectura le permite, dar nu sunt cerute în etapa
asta): weekly, every_x_weeks, days_of_week, monthly, days_of_month,
specific_dates. Un eveniment cu un recurrence_type necunoscut/neimplementat
pur și simplu nu are loc niciodată (occurs_on returnează False) - nu
aruncăm o excepție și nu presupunem un comportament implicit periculos.
"""

from datetime import date

DEFAULT_RECURRENCE_TYPE = "daily"


def _occurs_daily(event, check_date: date) -> bool:
    return check_date >= event.start_date


def _occurs_every_x_days(event, check_date: date) -> bool:
    if check_date < event.start_date:
        return False
    interval = event.recurrence_interval_days or 1
    if interval < 1:
        interval = 1
    return (check_date - event.start_date).days % interval == 0


# Nume -> funcție (event, date) -> bool. Ordinea contează doar pentru
# afișare (ex. în <select> din formular), nu pentru logică.
REGISTRY = {
    "daily": _occurs_daily,
    "every_x_days": _occurs_every_x_days,
}

# Etichete prietenoase pentru UI.
LABELS = {
    "daily": "Zilnic",
    "every_x_days": "La fiecare X zile",
}


def occurs_on(event, check_date: date) -> bool:
    """
    True dacă "event" are o apariție (occurrence) în ziua "check_date",
    conform tipului lui de recurență. Un tip necunoscut sau neimplementat
    înseamnă implicit "nu are loc" - nu presupunem nimic.
    """
    handler = REGISTRY.get(event.recurrence_type)
    if handler is None:
        return False
    return handler(event, check_date)


def is_known_recurrence_type(recurrence_type: str) -> bool:
    return recurrence_type in REGISTRY
