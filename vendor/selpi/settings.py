"""selpi settings shim — env-var based, no python-dotenv dependency."""
import os

def getb(key: bytes) -> bytes:
    return os.getenvb(b'SELPI_' + key)
