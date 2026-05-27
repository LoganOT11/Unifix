"""File-level encryption for work-order JSON output.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the ``cryptography`` library.
"""

import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

from .exceptions import CryptoError

logger = logging.getLogger("work_order_processor")

# Default key location — override with ENCRYPTION_KEY_PATH env var
DEFAULT_KEY_PATH = Path.home() / ".workorder_processor.key"


def get_fernet(key_path: str | None = None) -> Fernet:
    """
    Load or generate the symmetric encryption key.

    Reads from *key_path* (or ENCRYPTION_KEY_PATH, or ~/.workorder_processor.key).
    If the key file does not exist, generates a fresh key and writes it with 0o600.
    """
    path = Path(key_path or os.environ.get("ENCRYPTION_KEY_PATH", DEFAULT_KEY_PATH))

    if path.is_file():
        key = path.read_bytes()
        logger.debug("Loaded encryption key from %s", path)
    else:
        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        os.chmod(path, 0o600)
        logger.info("Generated new encryption key at %s (chmod 600)", path)

    return Fernet(key)


def write_encrypted_json(data: dict, out_path: str, key_path: str | None = None) -> str:
    """
    Serialize *data* to JSON and write it as an encrypted ``.json.enc`` file.

    Returns the path to the encrypted file.
    """
    fernet = get_fernet(key_path)
    plaintext = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")

    try:
        ciphertext = fernet.encrypt(plaintext)
    except Exception as exc:
        raise CryptoError(f"Encryption failed: {exc}") from exc

    enc_path = out_path + ".enc"
    Path(enc_path).write_bytes(ciphertext)
    os.chmod(enc_path, 0o640)
    logger.info("Encrypted output written to %s", enc_path)
    return enc_path


def read_encrypted_json(enc_path: str, key_path: str | None = None) -> dict:
    """Read and decrypt a ``.json.enc`` file back into a dict."""
    fernet = get_fernet(key_path)
    ciphertext = Path(enc_path).read_bytes()

    try:
        plaintext = fernet.decrypt(ciphertext)
    except Exception as exc:
        raise CryptoError(f"Decryption failed (wrong key or corrupted file): {exc}") from exc

    return json.loads(plaintext.decode("utf-8"))
