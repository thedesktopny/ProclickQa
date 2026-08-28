"""
Signing in with the CMS's own accounts.

The CMS stores passwords in AspNetUsers.PasswordHash using ASP.NET Identity's
format. Two versions exist and both appear in the wild:

  v2 (first byte 0x00): 0x00 | salt(16) | PBKDF2-HMAC-SHA1(password, salt,
                        1000 iterations, 32 bytes)
  v3 (first byte 0x01): 0x01 | prf(4) | iterations(4) | saltLen(4) |
                        salt | subkey            (all big-endian)

Verifying here means nobody needs a second password, and access follows the
accounts that already exist. Nothing is written and no password is stored.
"""
import base64
import hashlib
import hmac
import struct


def verify_password(password, stored_hash):
    """True if the password matches the stored ASP.NET Identity hash."""
    if not password or not stored_hash:
        return False
    try:
        blob = base64.b64decode(stored_hash)
    except Exception:
        return False
    if not blob:
        return False
    pwd = password.encode('utf-8')

    if blob[0] == 0x00:
        if len(blob) != 49:
            return False
        salt, expected = blob[1:17], blob[17:49]
        actual = hashlib.pbkdf2_hmac('sha1', pwd, salt, 1000, 32)
        return hmac.compare_digest(actual, expected)

    if blob[0] == 0x01:
        if len(blob) < 13:
            return False
        prf, iters, salt_len = struct.unpack('>III', blob[1:13])
        salt = blob[13:13 + salt_len]
        expected = blob[13 + salt_len:]
        algo = {0: 'sha1', 1: 'sha256', 2: 'sha512'}.get(prf)
        if not algo or not expected:
            return False
        actual = hashlib.pbkdf2_hmac(algo, pwd, salt, iters, len(expected))
        return hmac.compare_digest(actual, expected)

    return False


def make_hash_v2(password):
    """Only used to test the verifier against a hash of known format."""
    import os
    salt = os.urandom(16)
    sub = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), salt, 1000, 32)
    return base64.b64encode(b'\x00' + salt + sub).decode()


def make_hash_v3(password, iterations=10000):
    import os
    salt = os.urandom(16)
    sub = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations, 32)
    return base64.b64encode(b'\x01' + struct.pack('>III', 1, iterations, len(salt))
                            + salt + sub).decode()
