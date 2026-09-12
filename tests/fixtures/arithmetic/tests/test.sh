#!/bin/bash
set -eu
mkdir -p /logs/verifier
python - <<'PY'
from pathlib import Path
p=Path('/app/answer.txt')
score=int(p.exists() and p.read_text().strip()=='42')
Path('/logs/verifier/reward.txt').write_text(str(score))
PY
