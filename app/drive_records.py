"""Private Drive properties keep a publication record on its original PDF.

Drive permits 30 private properties per application and 124 UTF-8 bytes per
key/value pair. Keep the complete JSON, compressed and split within those
limits; never truncate certificate data or change the PDF bytes.
"""

import base64
import hashlib
import json
import zlib

MAX_RECORD_BYTES = 64 * 1024
PARTS = 27
PART_BYTES = 117  # Seven-byte key qcp_m00 + value <= 124 bytes.


def original_file(properties: dict, file_id: str) -> bool:
    # Drive copies can inherit appProperties. A copy is not the issued file.
    return isinstance(properties, dict) and properties.get("qcp_origin") == hashlib.sha256(file_id.encode()).hexdigest()


def encode_record(info: dict) -> dict[str, str]:
    raw = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode()
    encoded = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    if len(raw) > MAX_RECORD_BYTES or len(encoded) > PARTS * PART_BYTES:
        raise ValueError("El registro técnico supera la capacidad de propiedades privadas de Drive. No se publicó el certificado ni se recortaron sus datos.")
    properties = {
        "qcp_certificate_id": info["id"],
        "qcp_origin": hashlib.sha256(info["driveFileId"].encode()).hexdigest(),
        "qcp_record": "1",
    }
    properties.update({f"qcp_m{i:02}": encoded[start:start + PART_BYTES]
                       for i, start in enumerate(range(0, len(encoded), PART_BYTES))})
    return properties


def decode_record(properties: dict) -> dict:
    try:
        if properties.get("qcp_record") != "1":
            raise ValueError
        keys = sorted(key for key in properties if key.startswith("qcp_m"))
        if not keys or len(keys) > PARTS or keys != [f"qcp_m{i:02}" for i in range(len(keys))]:
            raise ValueError
        values = [properties[key] for key in keys]
        if any(not isinstance(value, str) or not 1 <= len(value.encode()) <= PART_BYTES for value in values):
            raise ValueError
        compressed = base64.b64decode("".join(values), validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError
        info = json.loads(raw)
        if not isinstance(info, dict):
            raise ValueError
        return info
    except (ValueError, TypeError, UnicodeError, zlib.error) as error:
        raise ValueError("El registro privado del certificado no es válido o está incompleto.") from error
