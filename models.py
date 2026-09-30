from datetime import date, datetime, timedelta

from extensions import db
from recurrence import DEFAULT_RECURRENCE_TYPE

# Implicite folosite doar dacă apelantul nu trece explicit `now`/`lease_seconds`
# (ex. apeluri directe din teste/consolă). scheduler.py trece mereu valorile
# citite din app.config (RETRY_LEASE_SECONDS) ca sursă unică de adevăr.
_DEFAULT_LEASE_SECONDS = 120


class Event(db.Model):
    """
    Un eveniment recurent, generic și data-driven (NU hardcodat pentru
    un anumit eveniment din joc).

    "start_date" este data de referință a recurenței (de la ce dată începe
    să conteze evenimentul, și - pentru recurențe de tip "every_x_days" -
    față de ce dată se calculează intervalul).

    "event_time" este ora din joc ("Server Time"), introdusă direct de
    utilizator, format "HH:MM", fără conversii per-membru Discord.

    "recurrence_type" + "recurrence_interval_days" descriu CÂND se repetă
    evenimentul. Vezi recurrence.py pentru motorul de recurență și pentru
    ce tipuri sunt implementate acum ("daily", "every_x_days") și cum se
    extinde arhitectura cu tipuri noi (weekly, monthly, date specifice...)
    fără să fie nevoie de schimbări în scheduler.

    Un eveniment poate avea ORICÂTE remindere (0, 1, 2, 5...) - vezi
    EventReminder mai jos. Nu presupunem un singur reminder per eveniment.

    IMPORTANT (etapa reliability): relația `reminders` de mai jos păstrează
    `cascade="all, delete-orphan"`. Asta ESTE corect pentru ștergerea
    EXPLICITĂ a unui EventReminder (db.session.delete(...) sau eliminarea
    lui din listă) - vezi models.EventReminder și app.py, ruta de editare.
    Ce NU mai facem e să golim toată colecția (`event.reminders.clear()`)
    doar pentru că se editează evenimentul - acela era bug-ul: "clear()" +
    delete-orphan ștergea TOATE EventReminder-urile (și, în cascadă,
    SentReminder-urile istorice ale lor) la orice editare, chiar dacă
    utilizatorul modifica doar ora sau mesajul. Editarea acum actualizează
    reminderele existente pe ID și adaugă doar cele noi - vezi app.py.
    """

    __tablename__ = "events"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)

    # Data de referință a recurenței. Implicit azi, dacă nu e specificată.
    start_date = db.Column(db.Date, nullable=False, default=date.today)

    # Format "HH:MM", 24h, în ora serverului de joc (fără conversii).
    event_time = db.Column(db.String(5), nullable=False)

    recurrence_type = db.Column(
        db.String(30), nullable=False, default=DEFAULT_RECURRENCE_TYPE
    )
    # Folosit doar de tipul "every_x_days" (interval în zile). Rămâne NULL
    # pentru alte tipuri de recurență.
    recurrence_interval_days = db.Column(db.Integer, nullable=True)

    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reminders = db.relationship(
        "EventReminder",
        backref="event",
        cascade="all, delete-orphan",
        order_by="EventReminder.minutes_before.desc()",
    )

    def __repr__(self):
        return f"<Event {self.name!r} @ {self.event_time} ({self.recurrence_type})>"


class EventReminder(db.Model):
    """
    Un reminder configurat pentru un eveniment: "trimite mesajul X cu Y
    minute înainte de ora evenimentului". Un Event poate avea mai multe
    EventReminder-uri (ex: 30 min / 15 min / 5 min înainte), fiecare cu
    propriul mesaj, complet independente unele de altele la trimitere.

    "active" permite dezactivarea temporară a UNUI singur reminder al unui
    eveniment, fără să afecteze celelalte remindere sau evenimentul însuși.

    ID-ul acestui rând e stabil pe durata de viață a reminderului - editarea
    formularului (app.py) identifică reminderele existente după acest ID ca
    să le actualizeze in-place, NU să le șteargă și recreeze (vezi Event,
    docstring, și punctul 3 din cerință).
    """

    __tablename__ = "event_reminders"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)

    minutes_before = db.Column(db.Integer, nullable=False, default=0)
    message = db.Column(db.Text, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # cascade="all, delete-orphan" e intenționat: ștergerea EXPLICITĂ a unui
    # reminder (utilizatorul bifează "șterge acest reminder" în formular)
    # șterge și istoricul SentReminder asociat NUMAI LUI. Acesta e un efect
    # documentat al unei acțiuni deliberate, nu un efect secundar al editării
    # generale a evenimentului (vezi punctul 3 din cerință) - editarea
    # normală (schimbare oră/mesaj/recurență) NU atinge deloc acest reminder
    # dacă nu e explicit marcat pentru ștergere.
    sent_reminders = db.relationship(
        "SentReminder", backref="event_reminder", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<EventReminder event_id={self.event_id} -{self.minutes_before}min>"


class SentReminder(db.Model):
    """
    Evidență persistentă + revendicare atomică pentru fiecare TRIMITERE
    posibilă a unui reminder: o combinație (event_reminder_id, occurrence_date).

    "occurrence_date" este data APARIȚIEI evenimentului (occurrence), NU
    data calendaristică a orei efective de trimitere a reminderului. Asta
    contează în special lângă miezul nopții: un eveniment la 00:05 cu un
    reminder de 15 minute înainte se trimite efectiv la 23:50 în ziua
    PRECEDENTĂ, dar rândul din SentReminder e asociat cu data evenimentului
    (occurrence_date), nu cu data lui 23:50. Astfel un singur eveniment de
    la miezul nopții nu se poate "coliza" cu ziua anterioară din greșeală,
    iar UNIQUE(event_reminder_id, occurrence_date) rămâne semantic corect:
    o singură trimitere per apariție a evenimentului, indiferent pe ce zi
    calendaristică cade efectiv ora de trimitere.

    "scheduled_for" este momentul exact (datetime, în APP_TIMEZONE) la care
    ar fi trebuit trimis reminderul - folosit de scheduler ca să decidă dacă
    mai e recuperabil (în RECOVERY_WINDOW_MINUTES) sau dacă a devenit "missed".

    Statusuri: pending (revendicat, în curs) -> sent | failed | missed.
      - pending: revendicat, trimiterea e în curs SAU procesul a murit
        înainte să scrie rezultatul ("pending stale" - vezi lease_until).
      - sent: livrat cu succes către Discord (best-effort - vezi "Exactly-once"
        mai jos, nu garantăm livrare matematic exactă).
      - failed: o încercare a eșuat, dar mai poate fi reîncercat
        (attempt_count < RETRY_MAX_ATTEMPTS și eroarea a fost clasificată
        "retryable" de webhook.py) - vezi next_retry_at.
      - missed: stare TERMINALĂ. Fie fereastra de recovery a expirat, fie
        s-au epuizat încercările, fie eroarea a fost clasificată "permanent"
        (ex. webhook invalid) - nu se mai reîncearcă.

    Câmpuri de retry/lease (etapa reliability, punctele 4 și 5):
      - attempt_count: câte revendicări (claim-uri) a avut acest rând, de la
        1 (claim inițial) în sus. Crescut la fiecare reîncercare reușită.
      - last_attempt_at: momentul ultimei încercări (succes sau eșec).
      - next_retry_at: momentul de la care rândul (dacă status=failed)
        devine eligibil pentru o nouă încercare - vezi retry_policy.py
        pentru formula de backoff.
      - lease_until: cât timp rămâne "activ" un claim pending, înainte să
        fie considerat abandonat (proces mort) și recuperabil de altcineva.
      - error_message: ultimul mesaj de eroare (trunchiat), pentru debugging
        - NU conține niciodată URL-ul webhook-ului (webhook.py nu-l expune).

    Ce garantăm și ce NU garantăm:
    - Garantăm prevenirea duplicatelor concurente (claim/reclaim atomic la
      nivel de bază de date - vezi claim_reminder mai jos) și tracking
      persistent/auditabil al fiecărui reminder, inclusiv istoricul de
      încercări (attempt_count, error_message).
    - NU garantăm "exactly once" matematic pentru livrarea efectivă către
      Discord: dacă procesul moare EXACT între "Discord a primit mesajul" și
      "am scris SENT în baza de date", un retry ulterior poate produce un
      mesaj Discord duplicat. Discord nu oferă idempotency externă pentru
      webhook-uri simple, deci această limitare e ireductibilă cu
      arhitectura curentă (HTTP + DB separate, fără tranzacție distribuită).
      Ce facem în schimb: minimizăm fereastra acestui caz (retry cu backoff,
      nu reîncercare imediată agresivă) și îl documentăm explicit, în loc să
      pretindem o garanție pe care n-o avem. Diferența e deci între:
        * DB-level exactly-once CLAIM (garantat - UNIQUE constraint) și
        * external delivery exactly-once (NEgarantat - limitare HTTP+DB).
    """

    __tablename__ = "sent_reminders"
    __table_args__ = (
        db.UniqueConstraint(
            "event_reminder_id", "occurrence_date", name="uq_event_reminder_occurrence"
        ),
        # Indexuri justificate de query-urile reale ale schedulerului/rapoartelor
        # (punctul 14 din cerință - fără indexuri "de rezervă" neutilizate):
        #   - status: schedulerul/rapoartele filtrează des după status.
        #   - next_retry_at: viitoarea interogare "ce e eligibil pt retry ACUM".
        #   - scheduled_for: rapoarte/debug pe interval de timp.
        #   - event_id: interogări "istoricul unui eveniment", fără join prin
        #     event_reminders (event_id e denormalizat exact pentru asta -
        #     vezi mai jos).
        db.Index("ix_sent_reminders_status", "status"),
        db.Index("ix_sent_reminders_next_retry_at", "next_retry_at"),
        db.Index("ix_sent_reminders_scheduled_for", "scheduled_for"),
        db.Index("ix_sent_reminders_event_id", "event_id"),
    )

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_MISSED = "missed"

    id = db.Column(db.Integer, primary_key=True)
    event_reminder_id = db.Column(
        db.Integer, db.ForeignKey("event_reminders.id"), nullable=False
    )
    # Denormalizat INTENȚIONAT (copie a event_reminder.event_id) - evită un
    # join suplimentar pentru interogări/rapoarte simple pe eveniment (ex.
    # "tot istoricul evenimentului X", fără să treci prin event_reminders).
    # E derivabil din event_reminder_id, deci există risc de inconsistență
    # DOAR dacă e setat manual, în afara claim_reminder() - fluxul normal
    # (claim_reminder) îl copiază mereu din reminder.event_id la creare și
    # nu îl modifică niciodată după aceea (occurrence-urile unui reminder nu
    # își schimbă evenimentul-părinte). Eliminarea completă a acestei
    # denormalizări ar cere o migrare structurală (renunțarea la coloană +
    # rescrierea tuturor query-urilor pe join) nejustificată pentru
    # beneficiul câștigat - păstrată conform punctului 13 din cerință.
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)

    occurrence_date = db.Column(db.Date, nullable=False)
    scheduled_for = db.Column(db.DateTime, nullable=False)

    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING)
    sent_at = db.Column(db.DateTime, nullable=True)
    claimed_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- câmpuri noi (etapa reliability - retry/lease) ---
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    next_retry_at = db.Column(db.DateTime, nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return (
            f"<SentReminder event_reminder_id={self.event_reminder_id} "
            f"occurrence={self.occurrence_date} status={self.status!r} "
            f"attempt={self.attempt_count}>"
        )


def claim_reminder(
    event_reminder_id: int,
    event_id: int,
    occurrence_date,
    scheduled_for,
    now: datetime = None,
    lease_seconds: int = None,
) -> bool:
    """
    Încearcă să revendice ATOMIC trimiterea reminderului "event_reminder_id"
    pentru apariția evenimentului din ziua "occurrence_date".

    Returnează True dacă procesul curent a revendicat reminderul ACUM (fie
    ca INSERT nou, fie ca reclaim al unui rând failed-retryabil sau
    pending-stale existent). Returnează False dacă reminderul e deja
    revendicat activ de altcineva, sau dacă e într-o stare terminală
    (sent/missed) care nu mai poate fi revendicată.

    Doi pași, ambii atomici la nivel de bază de date (niciodată
    SELECT -> verificare -> INSERT/UPDATE separate în cod Python):

    1. INSERT ... ON CONFLICT DO NOTHING - dacă rândul nu există încă
       (prima încercare pentru această apariție), inserarea reușește direct.
    2. Dacă a existat conflict (rândul exista deja), încearcă un
       UPDATE ... WHERE <e retryabil SAU stale> - vezi _try_reclaim.

    `now`/`lease_seconds` sunt parametri expliciți (nu `datetime.utcnow()`
    direct în funcție) ca toate testele să poată injecta un `now` controlat,
    fără să depindă de ceasul real al mașinii care rulează testele.
    """
    now = now if now is not None else datetime.utcnow()
    lease_seconds = lease_seconds if lease_seconds is not None else _DEFAULT_LEASE_SECONDS
    lease_until = now + timedelta(seconds=lease_seconds)

    values = dict(
        event_reminder_id=event_reminder_id,
        event_id=event_id,
        occurrence_date=occurrence_date,
        scheduled_for=scheduled_for,
        status=SentReminder.STATUS_PENDING,
        claimed_at=now,
        lease_until=lease_until,
        attempt_count=1,
        last_attempt_at=now,
    )

    if _try_insert(values):
        return True

    return _try_reclaim(event_reminder_id, occurrence_date, now, lease_until)


def _try_insert(values: dict) -> bool:
    """
    Pasul 1 al claim-ului: INSERT ... ON CONFLICT DO NOTHING, cu dialectul
    corect pentru motorul de bază de date curent (PostgreSQL sau SQLite -
    ambele suportă "ON CONFLICT DO NOTHING" nativ). Pentru alte motoare
    (fallback generic, nu ar trebui folosit în producție), un INSERT simplu
    + prinderea erorii de unicitate produce exact același rezultat observabil.
    """
    dialect = db.engine.dialect.name

    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        from sqlalchemy.exc import IntegrityError

        try:
            db.session.add(SentReminder(**values))
            db.session.commit()
            return True
        except IntegrityError:
            db.session.rollback()
            return False

    stmt = dialect_insert(SentReminder).values(**values)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["event_reminder_id", "occurrence_date"]
    )
    result = db.session.execute(stmt)
    db.session.commit()

    # rowcount == 1 -> rândul a fost INSERAT acum (revendicare reușită).
    # rowcount == 0 -> conflict, rândul exista deja (poate fi reclaim-uit).
    return result.rowcount == 1


def _try_reclaim(event_reminder_id: int, occurrence_date, now: datetime, lease_until: datetime) -> bool:
    """
    Pasul 2 al claim-ului: rândul exista deja (conflict la INSERT).
    Încearcă să-l REVENDICE dacă:
      (a) e "failed" ȘI a trecut de next_retry_at (retry eligibil - punctul 4
          din cerință), SAU
      (b) e "pending" ȘI a trecut de lease_until (claim abandonat, procesul
          anterior a murit înainte să scrie rezultatul - punctul 5 din
          cerință, "pending stale / lease").

    Rândurile "sent" sau "missed" NU se potrivesc niciunei condiții de mai
    sus, deci NU pot fi reclaim-uite - stări terminale, corect.

    De ce UPDATE ... WHERE e suficient (fără SELECT FOR UPDATE explicit):
    e ATOMIC la nivel de rând exact ca INSERT ... ON CONFLICT. Dacă
    Process A și Process B execută acest UPDATE simultan pe același rând,
    baza de date serializează cele două comenzi prin lock-ul de rând pe care
    UPDATE îl ia automat: primul UPDATE ia lock-ul, aplică schimbarea și
    comite; al doilea UPDATE așteaptă lock-ul, apoi RE-EVALUEAZĂ clauza WHERE
    față de starea deja comisă (status e acum "pending" cu alt lease_until,
    care nu se mai potrivește niciunei condiții) - deci afectează 0 rânduri.
    Doar UNUL dintre cele două procese poate avea rowcount == 1. Acest
    comportament e valabil atât pe PostgreSQL cât și pe SQLite (ambele indică
    lock exclusiv de rând/bază pentru UPDATE); pe SQLite, `PRAGMA
    busy_timeout` (vezi extensions.py) evită eroarea "database is locked"
    când al doilea proces trebuie doar să aștepte puțin lock-ul, nu să eșueze.
    """
    stmt = (
        SentReminder.__table__.update()
        .where(
            SentReminder.__table__.c.event_reminder_id == event_reminder_id,
            SentReminder.__table__.c.occurrence_date == occurrence_date,
            db.or_(
                db.and_(
                    SentReminder.__table__.c.status == SentReminder.STATUS_FAILED,
                    SentReminder.__table__.c.next_retry_at.isnot(None),
                    SentReminder.__table__.c.next_retry_at <= now,
                ),
                db.and_(
                    SentReminder.__table__.c.status == SentReminder.STATUS_PENDING,
                    SentReminder.__table__.c.lease_until.isnot(None),
                    SentReminder.__table__.c.lease_until <= now,
                ),
            ),
        )
        .values(
            status=SentReminder.STATUS_PENDING,
            claimed_at=now,
            lease_until=lease_until,
            attempt_count=SentReminder.__table__.c.attempt_count + 1,
            last_attempt_at=now,
        )
    )
    result = db.session.execute(stmt)
    db.session.commit()
    return result.rowcount == 1


def get_sent_reminder(event_reminder_id: int, occurrence_date):
    """Rândul SentReminder pentru (event_reminder_id, occurrence_date), sau None."""
    return SentReminder.query.filter_by(
        event_reminder_id=event_reminder_id, occurrence_date=occurrence_date
    ).first()
