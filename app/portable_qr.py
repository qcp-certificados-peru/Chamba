"""Self-contained signed publication records for folder-independent QR lookup.

The public key is pinned in the printed QR. The ephemeral private key is used
once and never stored in Drive, the PDF or the response. A private registry
retains the signed record independently of PDF copies and their metadata.
"""

import base64
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.drive_records import decode_record, encode_record

MODE = "portable-v1"
KEY = re.compile(r"[A-Za-z0-9_-]{43}")


def _encoded(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decoded(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def new_key() -> tuple[str, bytes]:
    private = Ed25519PrivateKey.generate()
    return _encoded(private.public_key().public_bytes_raw()), private.private_bytes_raw()


def _message(info: dict) -> bytes:
    return json.dumps(info, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def signed_record(info: dict, private: bytes) -> dict[str, str]:
    properties = encode_record(info)
    properties.pop("qcp_origin")
    properties["qcp_signature"] = _encoded(Ed25519PrivateKey.from_private_bytes(private).sign(_message(info)))
    return properties


def verified_record(properties: dict, identifier: str, public_key: str | None = None) -> dict:
    try:
        if not isinstance(properties, dict):
            raise ValueError
        info = decode_record(properties)
        key = info.get("qrPublicKey", "")
        if (info.get("id") != identifier or info.get("qrMode") != MODE or not KEY.fullmatch(key)
                or public_key is not None and key != public_key):
            raise ValueError
        url = info.get("verificationUrl")
        if not isinstance(url, str) or not url.endswith(f"/q/{identifier}/{key}"):
            raise ValueError
        Ed25519PublicKey.from_public_bytes(_decoded(key)).verify(_decoded(properties["qcp_signature"]), _message(info))
        return info
    except (ValueError, TypeError, KeyError, InvalidSignature) as error:
        raise ValueError("La copia no tiene un registro de emisión válido para este QR.") from error
