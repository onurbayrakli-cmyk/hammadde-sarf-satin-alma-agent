"""Run shipped unit tests with an isolated synthetic runtime bootstrap."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sentetik kaynak testleri; hedef bağlantı kabulü değildir")
    parser.add_argument("--suite", choices=("root", "components"), default="root")
    args = parser.parse_args(argv)
    if any(name in os.environ for name in ("SATINALMA_CONFIG", "SATINALMA_STATE_DIR")):
        print("Test sürecinde SATINALMA_CONFIG ve SATINALMA_STATE_DIR tanımlı olmamalı.", file=sys.stderr)
        return 2
    release = Path(__file__).resolve().parent.parent
    agent = release / "agent" if (release / "MANIFEST.json").is_file() else release
    if not (agent / "test_runtime_config.py").is_file():
        print("Test kaynakları bu klasörde bulunamadı.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(agent))
    if release != agent:
        sys.path.insert(1, str(release))
    runtime = importlib.import_module("runtime_config")
    if Path(runtime.__file__).resolve() != agent / "runtime_config.py":
        print("Test runtime kaynağı doğrulanamadı.", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="purchasing-unit-sandbox-") as folder:
        checkout = Path(folder).resolve() / "synthetic-checkout"
        synthetic = checkout / ".codex" / "agents" / "satin-alma-stok"
        synthetic.mkdir(parents=True)
        # Only unit-test imports use this synthetic development root. Shipped
        # files and MANIFEST remain intact; production config guards are not
        # weakened. Package-mode refusal is tested separately with a MANIFEST.
        with patch.object(runtime, "SOURCE_ROOT", synthetic):
            if args.suite == "root":
                # Parent tests exercise the legacy local-fixture branch. Child
                # processes cannot inherit this in-memory patch, so they get a
                # real external synthetic config, with no usable flow or key.
                for name in ("case_worker", "monitor_procurement", "rfq_worker"):
                    importlib.import_module(name)
        child_config = Path(folder).resolve() / "child-test-config.json"
        child_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state_dir": str(Path(folder).resolve() / "synthetic-state"),
                    "langflow": {
                        "base_url": "http://127.0.0.1:1",
                        "flow_id": None,
                        "api_key_env": "SYNTHETIC_TEST_API_KEY",
                        "database_path": None,
                    },
                }
            )
        )
        with (
            patch.dict(os.environ, {"SATINALMA_CONFIG": str(child_config)}),
            patch("socket.getaddrinfo", side_effect=AssertionError("Offline tests cannot use DNS")),
            patch("socket.create_connection", side_effect=AssertionError("Offline tests cannot connect")),
            patch("socket.socket.connect", side_effect=AssertionError("Offline tests cannot connect")),
        ):
            if args.suite == "root":
                monitor = importlib.import_module("monitor_procurement")
                monitor.PROJECT = checkout
            start = agent if args.suite == "root" else agent / "components"
            suite = unittest.defaultTestLoader.discover(str(start), pattern="test_*.py", top_level_dir=str(start))
            result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
