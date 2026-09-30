import logging
import os
from datetime import date
from types import SimpleNamespace

from flask import Flask, flash, redirect, render_template, request, url_for

from config import Config, validate_app_timezone, validate_scheduler_limits
from extensions import db, migrate
from forms import MAX_REMINDER_SLOTS, parse_reminder_slots, validate_event_form
from models import Event, EventReminder
from recurrence import LABELS as RECURRENCE_LABELS
from scheduler import start_scheduler
from webhook import send_discord_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Render/Heroku dau uneori URL-uri "postgres://", dar SQLAlchemy modern
    # cere "postgresql://". Fix mic, ca migrarea viitoare să fie fără fricțiuni.
    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_url.startswith("postgres://"):
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url.replace("postgres://", "postgresql://", 1)

    # Fail fast dacă APP_TIMEZONE nu e un fus orar IANA valid - vezi
    # config.validate_app_timezone (nu mai cădem silențios pe UTC).
    validate_app_timezone(app.config["APP_TIMEZONE"])
    validate_scheduler_limits(
        app.config["MAX_REMINDER_HORIZON_DAYS"],
        app.config["MAX_REMINDER_MINUTES_BEFORE"],
    )

    db.init_app(app)
    if migrate is not None:
        # Flask-Migrate (Alembic) - vezi README, secțiunea "Migrații", pentru
        # comenzile exacte (`flask db init/migrate/upgrade`). NU generăm aici
        # scripturi de migrare automate: mediul de dezvoltare folosit pentru
        # această etapă nu a putut instala/verifica Alembic (fără acces la
        # rețea) - vezi raportul însoțitor. Arhitectura e pregătită, dar
        # migrația inițială trebuie generată și verificată de tine local.
        migrate.init_app(app, db)

    with app.app_context():
        if app.config.get("DB_AUTO_CREATE", True):
            # NOTĂ IMPORTANTĂ despre migrări (punctul 20 din cerință):
            # db.create_all() NU e un sistem de migrații - creează DOAR
            # tabelele care nu există încă, NU modifică tabele existente cu
            # schemă veche (nu adaugă coloane noi, nu creează indexuri noi
            # pe un tabel deja existent). E util STRICT pentru bootstrap pe o
            # bază de date NOUĂ (fișier SQLite nou / schemă Postgres goală).
            # Pe o bază de date care are deja `sent_reminders` dintr-o etapă
            # anterioară (fără attempt_count/lease_until/etc.), asta NU
            # actualizează schema - e nevoie de `flask db upgrade` (vezi
            # README). Setează DB_AUTO_CREATE=false în producție odată ce ai
            # migrațiile Alembic funcționale, ca să eviți orice ambiguitate
            # despre ce a creat schema efectivă a bazei de date.
            db.create_all()

    register_routes(app)

    if os.environ.get("ENABLE_SCHEDULER", "true").lower() == "true":
        start_scheduler(app)

    return app


def _form_view(cleaned_event, reminders, event_id=None, extra_reminder_count=0):
    """
    Construiește obiectul folosit pentru a randa formularul: fie gol/prefill
    normal, fie repopulat cu ce a completat utilizatorul, atunci când
    validarea eșuează (ca să nu piardă datele introduse).

    `extra_reminder_count` > 0 înseamnă că evenimentul are mai multe
    remindere decât MAX_REMINDER_SLOTS - acestea NU sunt afișate/editabile
    în acest formular minimal, dar rămân neatinse în baza de date (vezi
    edit_event) - afișăm explicit un mesaj despre asta, ca să nu pară un bug.
    """
    padded = list(reminders) + [None] * (MAX_REMINDER_SLOTS - len(reminders))
    return SimpleNamespace(
        id=event_id,
        name=cleaned_event.get("name", ""),
        event_time=cleaned_event.get("event_time", ""),
        start_date=cleaned_event.get("start_date") or date.today(),
        recurrence_type=cleaned_event.get("recurrence_type", "daily"),
        recurrence_interval_days=cleaned_event.get("recurrence_interval_days"),
        active=cleaned_event.get("active", True),
        reminders=padded,
        extra_reminder_count=extra_reminder_count,
    )


def register_routes(app):
    @app.context_processor
    def inject_recurrence_labels():
        return {"recurrence_labels": RECURRENCE_LABELS}

    @app.route("/")
    def index():
        events = Event.query.order_by(Event.event_time.asc()).all()
        return render_template("index.html", events=events)

    @app.route("/events/new", methods=["GET", "POST"])
    def new_event():
        if request.method == "POST":
            cleaned, errors = validate_event_form(request.form)
            reminders, reminder_errors = parse_reminder_slots(
                request.form,
                max_minutes_before=app.config.get("MAX_REMINDER_MINUTES_BEFORE", 7 * 24 * 60),
            )
            errors += reminder_errors

            if errors:
                for err in errors:
                    flash(err, "error")
                return render_template(
                    "event_form.html", event=_form_view(cleaned, reminders)
                )

            event = Event(
                name=cleaned["name"],
                event_time=cleaned["event_time"],
                start_date=cleaned["start_date"],
                recurrence_type=cleaned["recurrence_type"],
                recurrence_interval_days=cleaned["recurrence_interval_days"],
                active=cleaned["active"],
            )
            for r in reminders:
                if r["delete"]:
                    continue  # nu ar trebui să apară la un eveniment nou, dar ignorăm defensiv
                event.reminders.append(
                    EventReminder(minutes_before=r["minutes_before"], message=r["message"])
                )
            db.session.add(event)
            db.session.commit()
            flash(f"Eveniment „{event.name}” adăugat.", "success")
            return redirect(url_for("index"))

        return render_template(
            "event_form.html",
            event=_form_view(
                {"recurrence_type": "daily", "active": True, "start_date": date.today()}, []
            ),
        )

    @app.route("/events/<int:event_id>/edit", methods=["GET", "POST"])
    def edit_event(event_id):
        event = Event.query.get_or_404(event_id)

        if request.method == "POST":
            cleaned, errors = validate_event_form(request.form)
            reminders_data, reminder_errors = parse_reminder_slots(
                request.form,
                max_minutes_before=app.config.get("MAX_REMINDER_MINUTES_BEFORE", 7 * 24 * 60),
            )
            errors += reminder_errors

            if errors:
                for err in errors:
                    flash(err, "error")
                return render_template(
                    "event_form.html", event=_form_view(cleaned, reminders_data, event_id=event.id)
                )

            # --- actualizare pe ID, NU "clear() + recreate" (punctul 3 din
            # cerință) ---
            # Reminderele EXISTENTE ale evenimentului (și, prin ele,
            # SentReminder-urile lor istorice) NU sunt niciodată atinse doar
            # pentru că evenimentul e editat. Le identificăm prin ID:
            #   - id cunoscut + delete=False -> UPDATE in-place (minutes/message)
            #   - id cunoscut + delete=True  -> ștergere EXPLICITĂ (deliberată)
            #   - id necunoscut (slot nou)   -> INSERT (reminder nou)
            # Reminderele evenimentului care nu apar deloc în formular (ex.
            # evenimentul are mai mult de MAX_REMINDER_SLOTS remindere -
            # vezi extra_reminder_count) rămân complet neatinse.
            existing_by_id = {r.id: r for r in event.reminders}
            tamper_errors = []

            for r in reminders_data:
                if r["id"] is not None and r["id"] not in existing_by_id:
                    tamper_errors.append(
                        f"Reminderul cu ID {r['id']} nu aparține acestui eveniment."
                    )

            if tamper_errors:
                for err in tamper_errors:
                    flash(err, "error")
                return render_template(
                    "event_form.html", event=_form_view(cleaned, reminders_data, event_id=event.id)
                )

            event.name = cleaned["name"]
            event.event_time = cleaned["event_time"]
            event.start_date = cleaned["start_date"]
            event.recurrence_type = cleaned["recurrence_type"]
            # Notă (punctul 11 din cerință): schimbarea recurenței afectează
            # doar apariițiile VIITOARE - occurs_on() e evaluat de scheduler
            # cu regula curentă la fiecare rulare, iar SentReminder-urile
            # deja scrise (istoricul) rămân neschimbate, orice s-ar întâmpla
            # cu recurrence_type/interval de-acum înainte.
            event.recurrence_interval_days = cleaned["recurrence_interval_days"]
            event.active = cleaned["active"]

            for r in reminders_data:
                if r["delete"]:
                    target = existing_by_id[r["id"]]
                    # Ștergere EXPLICITĂ (utilizatorul a bifat-o) - cascadează
                    # și SentReminder-urile ACESTUI reminder (documentat pe
                    # EventReminder.sent_reminders în models.py). E un
                    # comportament deliberat, nu un efect secundar al editării.
                    db.session.delete(target)
                elif r["id"] is not None:
                    target = existing_by_id[r["id"]]
                    target.minutes_before = r["minutes_before"]
                    target.message = r["message"]
                else:
                    event.reminders.append(
                        EventReminder(minutes_before=r["minutes_before"], message=r["message"])
                    )

            db.session.commit()
            flash(f"Eveniment „{event.name}” actualizat.", "success")
            return redirect(url_for("index"))

        all_reminders = list(event.reminders)
        visible_reminders = all_reminders[:MAX_REMINDER_SLOTS]
        extra_reminder_count = max(0, len(all_reminders) - MAX_REMINDER_SLOTS)

        existing_reminders = [
            {"id": r.id, "delete": False, "minutes_before": r.minutes_before, "message": r.message}
            for r in visible_reminders
        ]
        view = _form_view(
            {
                "name": event.name,
                "event_time": event.event_time,
                "start_date": event.start_date,
                "recurrence_type": event.recurrence_type,
                "recurrence_interval_days": event.recurrence_interval_days,
                "active": event.active,
            },
            existing_reminders,
            event_id=event.id,
            extra_reminder_count=extra_reminder_count,
        )
        return render_template("event_form.html", event=view)

    @app.route("/events/<int:event_id>/delete", methods=["POST"])
    def delete_event(event_id):
        event = Event.query.get_or_404(event_id)
        name = event.name
        # NOTĂ (punctul 12 din cerință): cascade delete e păstrat ca fiind
        # cea mai simplă soluție pentru această etapă - ștergerea unui Event
        # șterge și EventReminder-urile lui și, prin ele, TOT istoricul
        # SentReminder asociat. Comportament documentat, nu ascuns - dacă ai
        # nevoie să păstrezi istoricul unui eveniment șters, dezactivează-l
        # (butonul "Dezactivează") în loc să-l ștergi.
        db.session.delete(event)
        db.session.commit()
        flash(f"Eveniment „{name}” șters (inclusiv istoricul reminderelor lui).", "success")
        return redirect(url_for("index"))

    @app.route("/events/<int:event_id>/toggle", methods=["POST"])
    def toggle_event(event_id):
        event = Event.query.get_or_404(event_id)
        event.active = not event.active
        db.session.commit()
        return redirect(url_for("index"))

    @app.route("/test-discord", methods=["POST"])
    def test_discord():
        success, info = send_discord_message(
            "🔔 Test din panoul de administrare — webhook-ul funcționează."
        )
        if success:
            flash("Mesaj de test trimis cu succes pe Discord.", "success")
        else:
            flash(f"Eroare la trimiterea mesajului de test: {info}", "error")
        return redirect(url_for("index"))


app = create_app()

if __name__ == "__main__":
    # Debug NU mai e activ implicit - modul debug expune un debugger
    # interactiv (execuție de cod arbitrar) dacă aplicația e accesibilă
    # public, ceea ce e periculos pentru un panou expus pe internet (Render).
    # Pentru dezvoltare locală, setează explicit FLASK_DEBUG=true.
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=debug_mode)
