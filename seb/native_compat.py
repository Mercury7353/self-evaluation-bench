"""Narrow, semantics-preserving compatibility for native Responses requests."""
import json


def upstream_body(raw: bytes, model: str) -> bytes:
    """xAI rejects tool_choice even when auto/none and no tools are available.

    Codex's local context-summary requests use this combination. Remove only
    the redundant selector on the verified Grok route. Keep prompts, history,
    opaque state, tool-enabled requests, and other providers unchanged.
    Required/named tool choices are not weakened into optional tool use.
    """
    if model != 'grok-4.7':
        return raw
    body = json.loads(raw)
    if (isinstance(body, dict) and body.get('tools', []) == []
            and body.get('tool_choice') in ('auto', 'none')):
        del body['tool_choice']
        return json.dumps(body).encode()
    return raw
