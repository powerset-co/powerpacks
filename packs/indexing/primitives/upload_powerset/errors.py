"""Append credential-redacted upload diagnostics to the local errors.log."""

from __future__ import annotations

import json
import os
import re
import traceback
from pathlib import Path
from urllib.parse import unquote, urlsplit

import turbopuffer

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.indexing.primitives.upload_powerset.manifest import CHANGED_CHECK

SAFE_ERRORS = {
    "postgres_login": "Upload requires access to the powerset_v2 PostgreSQL schema",
    "local_index": "Upload requires a local search index; build the index first",
    "api_key": "Upload requires a TurboPuffer API key",
    "namespace": "Upload requires the shared TurboPuffer v3 namespaces",
    "operator": "no users row for the current Powerset credentials; run `$powerset login`",
}


def safe_error(error: BaseException, fallback: str) -> str:
    message = str(error)
    if message in SAFE_ERRORS.values() or message == CHANGED_CHECK:
        return message
    if re.fullmatch(r"Upload requires a current local index; \d+ shared people lack profiles", message):
        return message
    return fallback


def log_error(out_dir: Path, stage: str, error: BaseException) -> None:
    details = [f"{now_iso()} stage={stage}", ''.join(traceback.format_exception(error))]
    if isinstance(error, turbopuffer.APIStatusError):
        response = error.response
        request = response.request
        details.append(f"HTTP {error.status_code} {request.method} {request.url}")
        for header in ('x-request-id', 'request-id'):
            if value := response.headers.get(header):
                details.append(f"{header}: {value}")
        details.append('Response: ' + json.dumps(error.body, ensure_ascii=False, default=str))
    text = '\n'.join(details)
    secrets = {value for key, value in os.environ.items()
               if re.search(r'KEY|TOKEN|PASSWORD|SECRET|DATABASE_URL|DB_URL', key) and value}
    for value in tuple(secrets):
        if '://' in value:
            password = urlsplit(value).password
            if password:
                secrets.update((password, unquote(password)))
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(://[^\s/@:]+:)[^\s/@]+@', r'\1[REDACTED]@', text)
    out_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(out_dir / 'errors.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, 'a', encoding='utf-8') as log:
        log.write(text + '\n\n')
