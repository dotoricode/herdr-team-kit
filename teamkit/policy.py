"""Pure routing rules. Missing or expired quota never means zero usage."""
import math

CLAUDE_QUOTA_SOURCE = 'claude-statusline-response'


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def eligible(snapshot, now, ttl=120):
    if not isinstance(snapshot, dict) or snapshot.get('unavailable'):
        return False
    at = snapshot.get('observed_at')
    if not number(at) or not 0 <= now - at <= ttl:
        return False
    for key in ('five_hour', 'weekly'):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            return False
        used, reset = window.get('used'), window.get('resets_at')
        if not number(used) or not 0 <= used < 90 or not number(reset) or reset <= now:
            return False
    return True


def choose_account(team, personal, now, ttl=120, active=None):
    if active in ('team', 'personal'):
        return active
    for account, snapshot in (('team', team), ('personal', personal)):
        if eligible(snapshot, now, ttl):
            return account
    return None


def normalize_quota(provider, raw, now):
    result = {'observed_at': now}
    if provider == 'claude':
        rates = raw.get('rate_limits') or {}
        windows = [(key, rates.get(source)) for key, source in
                   (('five_hour', 'five_hour'), ('weekly', 'seven_day'))]
        for key, window in windows:
            if isinstance(window, dict):
                result[key] = {'used': window.get('used_percentage'), 'resets_at': window.get('resets_at')}
    elif provider == 'codex':
        rates = raw.get('rateLimitsByLimitId') or {}
        bucket = rates.get('codex') if rates else raw.get('rateLimits')
        if isinstance(bucket, dict):
            for window in (bucket.get('primary'), bucket.get('secondary')):
                if isinstance(window, dict):
                    key = {300:'five_hour', 10080:'weekly'}.get(window.get('windowDurationMins'))
                    if key:
                        result[key] = {'used': window.get('usedPercent'), 'resets_at': window.get('resetsAt')}
    else:
        raise ValueError('unsupported provider')
    return result


def context_action(used, safe_boundary):
    if not number(used) or not 0 <= used <= 100:
        return 'unknown'
    if used >= 70 and safe_boundary:
        return 'handoff'
    return 'prepare' if used >= 60 else 'keep'
