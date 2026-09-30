import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Clasificarea rezultatului unei livrări pe Discord, ca schedulerul să
# poată decide între "retry" și "eșec permanent" fără să parseze el însuși
# coduri HTTP (vezi punctul 18 din cerință).
CLASSIFICATION_SUCCESS = "success"
CLASSIFICATION_RETRYABLE = "retryable"
CLASSIFICATION_PERMANENT = "permanent"

# Coduri HTTP considerate erori PERMANENTE ale webhook-ului însuși (nu ale
# rețelei sau ale disponibilității temporare a Discord) - o reîncercare NU
# are cum să reușească fără intervenție manuală (webhook șters/invalid,
# payload respins definitiv de Discord).
_PERMANENT_STATUS_CODES = {400, 401, 403, 404, 410}


class DeliveryResult:
    """
    Rezultatul unei încercări de livrare pe Discord.

    Compatibil cu vechiul contract `(success, info) = send_discord_message(...)`
    prin `__iter__` - codul existent care despachetează un tuplu de 2
    elemente continuă să funcționeze neschimbat. Codul nou (scheduler.py)
    poate folosi în plus `.classification`, `.permanent`, `.status_code`,
    `.retry_after_seconds` pentru decizii de retry/backoff.
    """

    def __init__(
        self,
        success: bool,
        classification: str,
        detail: str,
        status_code: Optional[int] = None,
        retry_after_seconds: Optional[float] = None,
    ):
        self.success = success
        self.classification = classification
        self.detail = detail
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds

    @property
    def permanent(self) -> bool:
        return self.classification == CLASSIFICATION_PERMANENT

    @property
    def retryable(self) -> bool:
        return self.classification == CLASSIFICATION_RETRYABLE

    def __iter__(self):
        return iter((self.success, self.detail))

    def __repr__(self):
        return (
            f"<DeliveryResult success={self.success} "
            f"classification={self.classification!r} status_code={self.status_code}>"
        )


def get_webhook_url() -> str:
    return os.environ.get("DISCORD_WEBHOOK_URL", "")


def send_discord_message(content: str) -> DeliveryResult:
    """
    Trimite un mesaj pe Discord prin webhook.

    NU ridică excepții către caller și NU loghează niciodată URL-ul
    webhook-ului (doar rezultatul/eroarea) - orice eroare (rețea, timeout,
    HTTP) e capturată și transformată într-un DeliveryResult clasificat.
    """
    url = get_webhook_url()
    if not url:
        msg = "DISCORD_WEBHOOK_URL nu este setat (variabilă de mediu lipsă)."
        logger.error(msg)
        # Config lipsă e o eroare permanentă - retry automat nu o rezolvă.
        return DeliveryResult(False, CLASSIFICATION_PERMANENT, msg)

    try:
        response = requests.post(url, json={"content": content}, timeout=10)
    except requests.Timeout as exc:
        msg = f"Timeout la trimiterea webhook-ului: {exc}"
        logger.error(msg)
        return DeliveryResult(False, CLASSIFICATION_RETRYABLE, msg)
    except requests.RequestException as exc:
        msg = f"Eroare de rețea la trimiterea webhook-ului: {exc}"
        logger.error(msg)
        return DeliveryResult(False, CLASSIFICATION_RETRYABLE, msg)

    if response.status_code in (200, 204):
        logger.info("Mesaj Discord trimis cu succes.")
        return DeliveryResult(True, CLASSIFICATION_SUCCESS, "OK", status_code=response.status_code)

    if response.status_code == 429:
        retry_after = _parse_retry_after(response)
        msg = f"Discord rate-limit (429). Retry-After={retry_after}."
        logger.error(msg)
        return DeliveryResult(
            False,
            CLASSIFICATION_RETRYABLE,
            msg,
            status_code=429,
            retry_after_seconds=retry_after,
        )

    if response.status_code in _PERMANENT_STATUS_CODES:
        msg = f"Discord a răspuns cu status {response.status_code} (eroare permanentă): {response.text[:200]}"
        logger.error(msg)
        return DeliveryResult(False, CLASSIFICATION_PERMANENT, msg, status_code=response.status_code)

    # 5xx și orice alt cod necunoscut: tratat conservator ca retryable - mai
    # bine o reîncercare în plus decât să pierdem definitiv un reminder din
    # cauza unei erori tranzitorii de server.
    msg = f"Discord a răspuns cu status {response.status_code}: {response.text[:200]}"
    logger.error(msg)
    return DeliveryResult(False, CLASSIFICATION_RETRYABLE, msg, status_code=response.status_code)


def _parse_retry_after(response) -> Optional[float]:
    """Extrage header-ul Retry-After (secunde) dacă Discord îl furnizează la 429."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
