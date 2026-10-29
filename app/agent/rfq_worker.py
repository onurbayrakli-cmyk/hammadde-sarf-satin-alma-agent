"""Local supplier RFQ drafting/tracking CLI. No mail or model adapter is called."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from case_store import _no_secrets
from rfq_store import DEFAULT_OUTPUT as LEGACY_RFQ_OUTPUT, RFQStore
from runtime_config import get_runtime

RUNTIME = get_runtime()
DEFAULT_PATH = RUNTIME.queue_path
DEFAULT_OUTPUT = RUNTIME.output_dir if RUNTIME.configured else LEGACY_RFQ_OUTPUT

MAX_FILE_BYTES = 500_000


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON alan adları aynı nesnede yinelenemez")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("JSON yalnız sonlu sayılar içermeli")


def read_json_file(filename):
    path = Path(filename).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("JSON dosyası bulunamadı veya 500 KB sınırını aşıyor")
    with path.open("rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("JSON dosyası 500 KB sınırını aşıyor")
    try:
        parsed = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        _no_secrets(parsed)
        return parsed
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Dosya geçerli UTF-8 JSON içermeli") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Yerel teklif isteği taslakları ve kaynaklı yanıt takibi; e-posta göndermez")
    parser.add_argument("--queue", default=str(DEFAULT_PATH))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    subcommands = parser.add_subparsers(dest="command", required=True)
    prepare = subcommands.add_parser("prepare")
    prepare.add_argument("--request-id", required=True)
    prepare.add_argument("--suppliers", required=True)
    status = subcommands.add_parser("status")
    status.add_argument("--request-id")
    dispatch = subcommands.add_parser("record-dispatch")
    dispatch.add_argument("--rfq-id", required=True)
    dispatch.add_argument("--file", required=True)
    response = subcommands.add_parser("ingest-response")
    response.add_argument("--rfq-id", required=True)
    response.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    store = RFQStore(args.queue, args.output)
    try:
        if args.command == "status":
            result = store.status(args.request_id)
        elif args.command == "prepare":
            result = store.prepare(args.request_id, read_json_file(args.suppliers))
        elif args.command == "record-dispatch":
            result = store.record_dispatch(args.rfq_id, read_json_file(args.file))
        else:
            result = store.ingest_response(args.rfq_id, read_json_file(args.file))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        # Never echo source file contents or provider/credential configuration.
        print(json.dumps({"status": "error", "error": str(error), "emails_sent": False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
