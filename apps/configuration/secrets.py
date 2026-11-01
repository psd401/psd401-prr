# ABOUTME: Encryption for the write-only settings — a key the database never
# ABOUTME: holds, and a fingerprint that identifies a value without revealing it.
"""A database dump must not reveal a credential.

That is the whole requirement, and it decides the design. The ciphertext
lives in ``AppSecret``; the key that opens it lives only in the deployment's
environment, which is the same place ``SECRET_KEY`` and the database
password already live and is not in any backup of the database.

Two ways to supply that key, and the difference matters only to an operator:

``PRR_SETTINGS_ENCRYPTION_KEY``
    An explicit Fernet key. Use this when stored secrets must survive a
    ``SECRET_KEY`` rotation, or when the key is handed to the container from
    a secret store rather than typed into a file.

nothing
    The key is derived from ``SECRET_KEY`` with HKDF. This is the default so
    that a deployment gains encrypted settings without an extra bootstrap
    step it could forget. The cost is stated plainly: rotating
    ``SECRET_KEY`` makes every stored secret unreadable, and they must be
    entered again.

Either way the key is never written to the database, never returned by the
API, and never logged.

**Fingerprints.** The Settings page has to let an administrator answer "is
the value in front of me the one that is installed?" without reading it
back. A plain hash of a short secret is guessable offline by anyone holding
the database, so the fingerprint is an HMAC under the same key the
ciphertext needs: useless to a reader of the database alone, and stable for
anyone who can already decrypt.
"""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

#: Fixed HKDF parameters. They are not secret; the input key material is.
_HKDF_SALT = b"psd401-prr/app-settings/v1"
_HKDF_INFO = b"apps.configuration.secrets"

#: Derivation is deliberate work, so the derived key is reused per distinct
#: key material rather than per call. Keyed by the material itself, which
#: keeps ``override_settings`` in tests honest without a cache-busting hook.
_KEYS: dict[tuple[str, str], bytes] = {}


class SecretUnreadable(RuntimeError):
    """A stored secret cannot be decrypted with this deployment's key.

    The honest cause is almost always a rotated ``SECRET_KEY`` or a changed
    ``PRR_SETTINGS_ENCRYPTION_KEY``. Callers fail closed: the feature that
    needed the secret behaves exactly as it does when the secret is unset.
    """


def _key_material() -> tuple[str, str]:
    explicit = str(getattr(settings, "PRR_SETTINGS_ENCRYPTION_KEY", "") or "").strip()
    return explicit, str(settings.SECRET_KEY or "")


def _fernet_key() -> bytes:
    explicit, secret_key = _key_material()
    cached = _KEYS.get((explicit, secret_key))
    if cached is not None:
        return cached
    if explicit:
        try:
            Fernet(explicit.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                "PRR_SETTINGS_ENCRYPTION_KEY must be a Fernet key: 32 random "
                "bytes, url-safe base64 encoded."
            ) from exc
        key = explicit.encode("ascii")
    else:
        if not secret_key:
            raise ImproperlyConfigured(
                "A stored setting secret needs either "
                "PRR_SETTINGS_ENCRYPTION_KEY or a SECRET_KEY to derive from."
            )
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=_HKDF_INFO,
        ).derive(secret_key.encode("utf-8"))
        key = base64.urlsafe_b64encode(derived)
    _KEYS[(explicit, secret_key)] = key
    return key


def encrypt(plaintext: str) -> str:
    """The stored form of one secret."""
    return Fernet(_fernet_key()).encrypt(str(plaintext).encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """The plaintext, or :class:`SecretUnreadable` if this key cannot open it."""
    try:
        opened = Fernet(_fernet_key()).decrypt(str(ciphertext).encode("ascii"))
    except (InvalidToken, ValueError, TypeError, ImproperlyConfigured):
        # ImproperlyConfigured is a malformed PRR_SETTINGS_ENCRYPTION_KEY. It
        # opens nothing, exactly like a rotated key, so a reader fails closed
        # the same way instead of turning every sign-in into a server error.
        # Never chain: the provider's message can quote the token.
        raise SecretUnreadable(
            "A stored setting secret could not be decrypted with this "
            "deployment's key. Set it again in the application."
        ) from None
    return opened.decode("utf-8")


def fingerprint(plaintext: str) -> str:
    """A short, key-bound identifier for a value nobody may read back."""
    digest = hmac.new(
        _fernet_key(),
        str(plaintext).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:12]
