"""Select a handoff reason from existing bound observations without provider calls."""
import math
import re

from .config import cached_quota
from .policy import CLAUDE_NATIVE_QUOTA_SOURCE, CLAUDE_QUOTA_SOURCE, CODEX_QUOTA_SOURCE

CONTEXT_TTL = 120
SESSION = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
ACCOUNT_SOURCES = {'codex': {CODEX_QUOTA_SOURCE},
                   'claude': {CLAUDE_QUOTA_SOURCE, CLAUDE_NATIVE_QUOTA_SOURCE}}
CONTEXT_SOURCES = {'codex': 'codex-rollout', 'claude': CLAUDE_QUOTA_SOURCE}


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _fresh(at, now, ttl):
    return (_number(at) and _number(now) and _number(ttl) and ttl > 0
            and 0 <= now - at <= ttl)


def _high(used):
    return _number(used) and 90 <= used <= 100


def trigger(config, store, target, context_observation, now):
    """Prefer native context pressure, then the current account's usage or block.

    Each result retains its evidence's original timestamp and metric. An unknown
    usage reading or failed query is never interpreted as an explicit account block.
    """
    if not isinstance(target, dict) or not _number(now):
        return None
    provider, account = target.get('provider'), target.get('account')
    if (not isinstance(provider, str) or not isinstance(account, str) or provider not in ACCOUNT_SOURCES
            or account not in config.get('profiles', {}).get(provider, {})):
        return None
    observation = context_observation
    if isinstance(observation, dict):
        session = observation.get('session_id')
        if (observation.get('source') == CONTEXT_SOURCES[provider]
                and not observation.get('unavailable')
                and isinstance(session, str) and SESSION.fullmatch(session)
                and (not target.get('session_id') or target['session_id'] == session)
                and _fresh(observation.get('observed_at'), now, CONTEXT_TTL)
                and _high(observation.get('used'))):
            return {**observation, 'metric': 'context'}
    try:
        quota = cached_quota(config, store, provider, account)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if (not isinstance(quota, dict) or not isinstance(quota.get('source'), str)
            or quota['source'] not in ACCOUNT_SOURCES[provider]
            or not _fresh(quota.get('observed_at'), now, config.get('quota_ttl_seconds', 120))):
        return None
    evidence = {'observed_at': quota['observed_at'], 'source': quota['source'],
                'provider': provider, 'account': account}
    for key in ('five_hour', 'weekly'):
        window = quota.get(key)
        if not isinstance(window, dict) or not _high(window.get('used')):
            continue
        reset = window.get('resets_at')
        if quota['source'] != CLAUDE_NATIVE_QUOTA_SOURCE and (not _number(reset) or reset <= now):
            continue
        return {**evidence, 'metric': key, 'used': window['used']}
    if provider == 'codex' and quota.get('ordinary_usage_allowed') is False:
        return {**evidence, 'metric': 'account_blocked', 'used': None, 'blocked': True}
    return None
