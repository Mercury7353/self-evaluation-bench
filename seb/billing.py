"""Cache-adjusted usage estimates alongside conservative budget accounting."""
from .gateway import cost


def cache_adjusted_cost(usage, price):
    if 'input_tokens_details' in usage:
        from .native_responses import usage_cost
        return usage_cost(usage, price, cached=True)
    conservative=cost(usage,price)
    if conservative is None:return None
    if 'input_tokens' in usage:
        cached=usage.get('cache_read_input_tokens',0)
        context=usage.get('input_tokens',0)+cached+usage.get('cache_creation_input_tokens',0)
    else:
        cached=usage.get('prompt_tokens_details',{}).get('cached_tokens',0)
        context=usage.get('prompt_tokens',0)
    if not cached:return conservative
    # Unknown discount remains unknown, not silently promoted to an invoice estimate.
    if 'cache_read_multiplier' not in price:return None
    long=price.get('long_context',{})
    multiplier=long.get('input_multiplier',1) if context>long.get('threshold_tokens',float('inf')) else 1
    return conservative-cached*price['input']*(1-price['cache_read_multiplier'])*multiplier/1_000_000
