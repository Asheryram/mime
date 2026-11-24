"""
Encryption for invited users' secrets: their Gmail app password and their
Exa and Apify keys. Stored in the private state repo only as ciphertext
(tenants/<id>/secrets.enc), readable only with SECRETS_KEY, which lives in the
dashboard's environment (Vercel) and the pipeline's (a GitHub Actions secret).

The dashboard never shows a stored secret back: it only says whether one is
set, so a stolen session can replace a friend's key but never read it.
Shared by the dashboard and tenants.py (which imports it from webapp/).
"""
import json
import os

from cryptography.fernet import Fernet, InvalidToken

SECRET_FIELDS = ("gmail_app_password", "exa_api_key", "apify_api_token")


class SecretsUnavailable(RuntimeError):
    """SECRETS_KEY isn't configured, or doesn't match what the secrets were saved with."""


def _fernet():
    key = os.environ.get("SECRETS_KEY", "").strip()
    if not key:
        raise SecretsUnavailable("SECRETS_KEY is not set")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecretsUnavailable("SECRETS_KEY is not a valid key") from e


def available():
    try:
        _fernet()
        return True
    except SecretsUnavailable:
        return False


def encrypt(secrets):
    clean = {k: v for k, v in secrets.items() if k in SECRET_FIELDS and v}
    return _fernet().encrypt(json.dumps(clean).encode())


def decrypt(blob):
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob).decode())
    except InvalidToken as e:
        raise SecretsUnavailable("these secrets were saved with a different SECRETS_KEY") from e


def new_key():
    return Fernet.generate_key().decode()
