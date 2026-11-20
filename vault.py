"""Encryption for the logins kept in the CMS database.

Three places hold logins: customers' Personal Info (AccountPersonalInfo),
customers' stored logins (CustomerCredentialData), and the company's
Info & Logins page (CompanyInfo). From Nov 2026 their values are stored
encrypted, so someone who can open the database (or a database backup, or the
old CMS's emailed CSV backup) sees only scrambled text. Only this server, with
the key in its Azure settings, can turn them back into the real values — and it
does that only for someone signed in to the CMS.

  CMS_VAULT_KEY      the key: 32 random bytes, base64 (Azure → Environment variables)
  CMS_VAULT_KEY_OLD  optional — the previous key, while changing to a new one

An encrypted value looks like  enc1:<base64>  (AES-256-GCM: a fresh random
nonce per value, and a check that the value was not altered). Anything without
that start is an old plain value and is shown as it is until the sweep
encrypts it.

LOSING THE KEY MEANS LOSING THE LOGINS. Keep a copy somewhere safe.
"""
import base64
import os
import secrets

PREFIX = 'enc1:'
_NONCE = 12


def _decode_key(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        k = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    except Exception:
        try:
            k = base64.b64decode(raw + '=' * (-len(raw) % 4))
        except Exception:
            return None
    return k if len(k) == 32 else None


def _key():
    return _decode_key(os.getenv('CMS_VAULT_KEY'))


def _old_key():
    return _decode_key(os.getenv('CMS_VAULT_KEY_OLD'))


def key_problem():
    """None when the key is usable, otherwise what is wrong with it."""
    raw = (os.getenv('CMS_VAULT_KEY') or '').strip()
    if not raw:
        return 'CMS_VAULT_KEY is not set in Azure'
    if _key() is None:
        return 'CMS_VAULT_KEY is set but is not a 32-byte base64 key'
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception:
        return 'the "cryptography" package is not installed'
    return None


def enabled():
    return key_problem() is None


def is_encrypted(v):
    return isinstance(v, str) and v.startswith(PREFIX)


def encrypt(v):
    """The stored form of a value. Unchanged when there is no key yet (so
    nothing breaks before the key is set), when empty, or when it already is."""
    if v is None or v == '' or is_encrypted(v) or not enabled():
        return v
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(_NONCE)
    ct = AESGCM(_key()).encrypt(nonce, str(v).encode('utf-8'), None)
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode('ascii').rstrip('=')


LOCKED_NO_KEY = '(locked — the encryption key is missing on the server; tell David)'
LOCKED_BAD = '(locked — this could not be unlocked with the current key; tell David)'


def decrypt(v):
    """The real value. Plain (old) values pass straight through."""
    if not is_encrypted(v):
        return v
    keys = [k for k in (_key(), _old_key()) if k]
    if not keys:
        return LOCKED_NO_KEY
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = v[len(PREFIX):]
        blob = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    except Exception:
        return LOCKED_BAD
    for k in keys:
        try:
            return AESGCM(k).decrypt(blob[:_NONCE], blob[_NONCE:], None).decode('utf-8')
        except Exception:
            continue
    return LOCKED_BAD


def stored_length(v):
    """How many characters the encrypted form of v takes in the database."""
    n = _NONCE + len(str(v or '').encode('utf-8')) + 16
    return len(PREFIX) + (4 * n + 2) // 3


def needs_new_key(v):
    """True for a value still encrypted with the OLD key (to re-encrypt)."""
    if not is_encrypted(v) or not _old_key() or not _key():
        return False
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = v[len(PREFIX):]
        blob = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
        AESGCM(_key()).decrypt(blob[:_NONCE], blob[_NONCE:], None)
        return False
    except Exception:
        return True


def new_key():
    """A fresh key, for setting CMS_VAULT_KEY."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
