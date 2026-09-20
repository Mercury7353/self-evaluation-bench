#!/usr/bin/env python3
"""Verify a privately transferred frozen input tree, without model calls."""
import argparse
import hashlib
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--inputs", type=Path, required=True)
p.add_argument("--manifest", type=Path, required=True)
a = p.parse_args()
root = a.inputs.resolve()
manifest = json.loads(a.manifest.read_text())
expected = manifest["files"]
actual = {str(f.relative_to(root)) for f in root.rglob("*") if f.is_file()}
errors = []
for name, meta in expected.items():
    f = root / name
    if not f.resolve().is_relative_to(root):
        errors.append(f"outside root: {name}")
        continue
    if not f.is_file():
        errors.append(f"missing: {name}")
        continue
    h = hashlib.sha256()
    with f.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    if f.stat().st_size != meta["bytes"] or h.hexdigest() != meta["sha256"]:
        errors.append(f"mismatch: {name}")
errors += [f"unexpected: {name}" for name in sorted(actual - set(expected))]
print(json.dumps({"files": len(expected), "valid": not errors, "errors": errors}, indent=2))
raise SystemExit(1 if errors else 0)
