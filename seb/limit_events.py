"""Persist host-side deadline evidence independently of researcher-written traces."""
import json
from pathlib import Path
import time
import uuid

from fastapi.responses import JSONResponse


def deadline_response(config, entry):
    if entry.get('wallet') == 'designer':
        root = Path(config['artifacts']) / 'limit-events'
        root.mkdir(exist_ok=True)
        ident = uuid.uuid4().hex
        temporary = root / (ident + '.tmp')
        temporary.write_text(json.dumps({'reason': 'research_time_limit', 'wallet': 'designer',
                                        'models': entry['models'], 'at': time.time(),
                                        'deadline_epoch': entry['deadline_epoch']}))
        temporary.replace(root / (ident + '.json'))
    return JSONResponse({'error': {'type': 'deadline_exceeded',
                                  'message': 'The execution deadline has passed'}}, 409)
