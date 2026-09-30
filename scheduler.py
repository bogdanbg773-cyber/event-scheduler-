import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from extensions import db
from models import SentReminder, Event, claim_reminder, get_sent_reminder
from recurrence import occurs_on
from retry_policy import (
    candidate_occurrence_dates,
    compute_backoff_seconds,
    event_horizon_days,
    is_within_recovery_window,
    lateness_minutes,
)
from webhook import send_discord_message

logger = logging.getLogger(__name__)

_scheduler = None  # instanță unică per proces


def _get_tz(app) -> ZoneInfo:
    """
    Returnează fusul orar configurat prin APP_TIMEZONE.

    APP_TIMEZONE este deja validat la pornirea aplicației (vezi
    config.validate_app_timezone, apelat din app.create_app). Dacă totuși
    ajunge aici o valoare invalidă (de ex. modificată direct în mediu după
    pornire), NU o tratăm silențios ca UTC: logăm o eroare clară și
    propagăm excepția, ca problema să fie vizibilă imediat, nu ascunsă.
    """
    tz_name = app.config.get("APP_TIMEZONE", "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:
        logger.error(
            "APP_TIMEZONE=%r este invalid. NU folosesc UTC silențios; "
            "reminder-ele NU vor fi verificate până nu se corectează configurația.",
            tz_name,
        )
        raise RuntimeError(f"APP_TIMEZONE invalid: {tz_name!r}") from exc


def _compute_scheduled_for(occurrence_date, event_time_str, minutes_before):
    """
    Momentul exact (naiv, în ora locală a APP_TIMEZONE) la care ar trebui
    trimis reminderul pentru apariția "occurrence_date" a evenimentului.

    Scădem minutele direct dintr-un datetime "naiv" (fără tz atașat, dar
    calculat în ora locală configurată) - aritmetica standard de
    date/timedelta din Python face automat trecerea peste miezul nopții
    (occurrence_date la 00:05 minus 15 minute = occurrence_date - 1 zi,
    23:50), fără cod special pentru acest caz.
    """
    event_time_obj = datetime.strptime(event_time_str, "%H:%M").time()
    event_dt = datetime.combine(occurrence_date, event_time_obj)
    return event_dt - timedelta(minutes=minutes_before)


def _mark_missed(row, reason: str):
    row.status = SentReminder.STATUS_MISSED
    row.next_retry_at = None
    row.error_message = reason[:500] if reason else None
    db.session.commit()


def _handle_failure(reminder, row, result, now, retry_cfg):
    """
    Reminderul a fost revendicat, dar trimiterea pe Discord a eșuat.
    Decide între "failed" (mai poate fi reîncercat) și "missed" (terminal):

      - dacă webhook.py a clasificat eroarea ca PERMANENTĂ (ex. webhook
        invalid/șters) -> missed direct, indiferent de attempt_count -
        reîncercarea unei erori permanente nu are cum să reușească.
      - dacă attempt_count a atins RETRY_MAX_ATTEMPTS -> missed (retry-urile
        s-au epuizat).
      - altfel -> failed, cu next_retry_at calculat prin backoff exponențial
        (retry_policy.compute_backoff_seconds), respectând și un eventual
        Retry-After primit de la Discord la 429 (folosim maximul dintre
        backoff-ul nostru și Retry-After, ca să nu lovim din nou rate-limit).
    """
    row.error_message = (result.detail or "")[:500]

    if result.permanent:
        _mark_missed(row, result.detail)
        logger.error(
            "Reminder pentru evenimentul %r: eroare PERMANENTĂ, nu se mai reîncearcă. %s",
            reminder.event.name, result.detail,
        )
        return

    if row.attempt_count >= retry_cfg["max_attempts"]:
        _mark_missed(row, f"Epuizat după {row.attempt_count} încercări: {result.detail}")
        logger.error(
            "Reminder pentru evenimentul %r: epuizat după %s încercări, marcat 'missed'.",
            reminder.event.name, row.attempt_count,
        )
        return

    backoff_seconds = compute_backoff_seconds(
        row.attempt_count, retry_cfg["backoff_base"], retry_cfg["backoff_max"]
    )
    if getattr(result, "retry_after_seconds", None):
        backoff_seconds = max(backoff_seconds, result.retry_after_seconds)

    row.status = SentReminder.STATUS_FAILED
    row.next_retry_at = now + timedelta(seconds=backoff_seconds)
    db.session.commit()
    logger.warning(
        "Reminder pentru evenimentul %r: încercarea #%s a eșuat (%s). "
        "Reîncercare posibilă după %s (peste %.0fs).",
        reminder.event.name, row.attempt_count, result.detail,
        row.next_retry_at, backoff_seconds,
    )


def _process_reminder(reminder, occurrence_date, scheduled_for, now, recovery_window_minutes, retry_cfg):
    """
    Procesează UN singur (EventReminder, occurrence_date) care e scadent
    (scheduled_for <= now). Revendică atomic (claim nou SAU reclaim al unui
    failed-retryabil / pending-stale), apoi:
      - dacă am depășit fereastra de recovery -> "missed" direct, fără să
        mai încercăm trimiterea (ar ajunge oricum prea târziu ca să mai
        aibă sens un anunț pe Discord) - se aplică ȘI la retry-uri: un
        reminder care a tot eșuat și a ajuns între timp peste fereastră nu
        mai e reîncercat la infinit.
      - altfel -> trimite pe Discord și actualizează statusul (sent / failed
        cu backoff / missed dacă eroarea era permanentă sau încercările
        s-au epuizat - vezi _handle_failure).
    """
    claimed = claim_reminder(
        event_reminder_id=reminder.id,
        event_id=reminder.event_id,
        occurrence_date=occurrence_date,
        scheduled_for=scheduled_for,
        now=now,
        lease_seconds=retry_cfg["lease_seconds"],
    )
    if not claimed:
        return  # deja revendicat activ de altcineva, sau stare terminală

    row = get_sent_reminder(reminder.id, occurrence_date)
    if row is None:
        logger.error(
            "Claim raportat ca reușit, dar rândul SentReminder nu a fost găsit "
            "(event_reminder_id=%s, occurrence=%s) - stare neașteptată.",
            reminder.id, occurrence_date,
        )
        return

    if not is_within_recovery_window(now, scheduled_for, recovery_window_minutes):
        late_min = lateness_minutes(now, scheduled_for)
        logger.warning(
            "Reminder pentru evenimentul %r (occurrence=%s) a depășit fereastra de "
            "recovery (%.1f min întârziere > %s min) - marcat 'missed', nu se mai trimite.",
            reminder.event.name, occurrence_date, late_min, recovery_window_minutes,
        )
        _mark_missed(row, f"Peste fereastra de recovery ({late_min:.1f} min întârziere).")
        return

    label = "reîncercare" if row.attempt_count > 1 else "prima încercare"
    logger.info(
        "Reminder revendicat (%s, attempt #%s) pentru evenimentul %r - trimit pe Discord.",
        label, row.attempt_count, reminder.event.name,
    )
    result = send_discord_message(reminder.message)

    if result.success:
        row.status = SentReminder.STATUS_SENT
        row.sent_at = datetime.utcnow()
        row.error_message = None
        db.session.commit()
        logger.info("Reminder trimis cu succes pentru %r.", reminder.event.name)
        return

    _handle_failure(reminder, row, result, now, retry_cfg)


def check_events(app):
    """
    Job-ul rulat periodic. Complet independent de rutele Flask: poate fi
    apelat și dintr-un proces separat (worker) dacă e nevoie în viitor.

    Pentru fiecare eveniment activ, pentru fiecare reminder activ al lui, și
    pentru fiecare apariție candidată (occurrence_date), calculăm momentul
    exact la care ar fi trebuit trimis reminderul (scheduled_for). Dacă acel
    moment a trecut deja (scheduled_for <= now), încercăm să revendicăm și
    să procesăm reminderul.

    Occurrence-urile candidate NU mai sunt fixate la "ieri/azi/mâine"
    (vezi punctul 9 din cerință - problema ±1 zi): pentru fiecare eveniment,
    orizontul înainte se calculează din cel mai îndepărtat reminder activ al
    LUI (retry_policy.event_horizon_days), plafonat de MAX_REMINDER_HORIZON_DAYS
    ca plasă de siguranță. Orizontul înapoi (REMINDER_LOOKBACK_DAYS) acoperă
    recovery după o repornire/adormire mai lungă și cazurile de lângă miezul
    nopții. Asta rămâne o soluție intenționat simplă (fără tabel Occurrence
    materializat - vezi punctul 10 din cerință), dar nu mai e legată rigid
    de ±1 zi, deci suportă și viitoare recurențe cu reminder-e mai îndepărtate
    fără nicio schimbare aici.

    Reminderele viitoare (scheduled_for > now) sunt pur și simplu ignorate
    la verificarea curentă - vor fi văzute din nou la următoarea rulare.
    """
    with app.app_context():
        try:
            tz = _get_tz(app)
        except RuntimeError:
            return  # eroare deja logată în _get_tz; nu continuăm verificarea

        now = datetime.now(tz).replace(tzinfo=None)  # naiv, în ora locală configurată
        recovery_window_minutes = app.config.get("RECOVERY_WINDOW_MINUTES", 15)
        lookback_days = app.config.get("REMINDER_LOOKBACK_DAYS", 1)
        max_horizon_days = app.config.get("MAX_REMINDER_HORIZON_DAYS", 7)
        retry_cfg = {
            "max_attempts": app.config.get("RETRY_MAX_ATTEMPTS", 5),
            "backoff_base": app.config.get("RETRY_BACKOFF_BASE_SECONDS", 20),
            "backoff_max": app.config.get("RETRY_BACKOFF_MAX_SECONDS", 600),
            "lease_seconds": app.config.get("RETRY_LEASE_SECONDS", 120),
        }
        today = now.date()

        logger.info("Scheduler check @ %s (server time, tz=%s)", now.strftime("%Y-%m-%d %H:%M"), tz)

        events = Event.query.filter_by(active=True).all()
        for event in events:
            if not event.event_time or len(event.event_time) != 5:
                logger.error("Eveniment %r are ora invalidă: %r", event.name, event.event_time)
                continue

            active_reminders = [r for r in event.reminders if r.active]
            if not active_reminders:
                continue

            max_minutes_before = max(r.minutes_before for r in active_reminders)
            horizon_days = event_horizon_days(max_minutes_before, max_horizon_days)

            for occurrence_date in candidate_occurrence_dates(today, lookback_days, horizon_days):
                if not occurs_on(event, occurrence_date):
                    continue

                for reminder in active_reminders:
                    try:
                        scheduled_for = _compute_scheduled_for(
                            occurrence_date, event.event_time, reminder.minutes_before
                        )
                    except ValueError:
                        logger.error(
                            "Eveniment %r are ora invalidă: %r", event.name, event.event_time
                        )
                        continue

                    if scheduled_for > now:
                        continue  # încă nu e scadent

                    _process_reminder(
                        reminder, occurrence_date, scheduled_for, now,
                        recovery_window_minutes, retry_cfg,
                    )


def start_scheduler(app):
    """
    Pornește scheduler-ul o singură dată per proces.

    Rulăm prima verificare IMEDIAT la pornire (next_run_time=acum), nu
    abia după primul interval - esențial pentru recovery pe Render Free:
    dacă serviciul a fost adormit și pornește la 17:50, vrem să verificăm
    reminderele restante chiar în acel moment, nu să așteptăm încă
    SCHEDULER_INTERVAL_SECONDS până la prima verificare.

    NOTĂ (punctul 22 din cerință): arhitectura recomandată rămâne UN singur
    worker/proces cu schedulerul activ. `claim_reminder()` (models.py) e
    sigur chiar dacă două procese pornesc accidental (claim/reclaim atomic
    la nivel de bază de date), dar NU construim aici un distributed lock -
    separarea explicită web/worker e o etapă viitoare, nu implicită acum.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    interval = app.config.get("SCHEDULER_INTERVAL_SECONDS", 30)

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        func=check_events,
        args=[app],
        trigger="interval",
        seconds=interval,
        id="check_events_job",
        replace_existing=True,
        next_run_time=datetime.now(),
    )
    _scheduler.start()
    logger.info(
        "Scheduler pornit (interval=%ss, recovery_window=%smin).",
        interval, app.config.get("RECOVERY_WINDOW_MINUTES", 15),
    )
    return _scheduler
