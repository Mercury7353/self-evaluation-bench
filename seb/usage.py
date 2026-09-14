"""Interpret token counts without assuming all chat providers include thinking."""


def chat_output_tokens(usage):
    """Return billable output, or None when reasoning inclusion is ambiguous."""
    details = usage.get('completion_tokens_details') or {}
    if not isinstance(details, dict):
        return None
    prompt = usage.get('prompt_tokens')
    output = usage.get('completion_tokens')
    reasoning = details.get('reasoning_tokens') or 0
    if any(type(n) is not int or n < 0 for n in (prompt, output, reasoning)):
        return None
    total = usage.get('total_tokens')
    if total is None:
        return output if reasoning == 0 else None
    if type(total) is not int or total < 0:
        return None
    if total == prompt + output and reasoning <= output:
        return output
    if reasoning > 0 and total == prompt + output + reasoning:
        return output + reasoning
    return None
