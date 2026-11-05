"""Pure routing rules. Missing or expired quota never means zero usage."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLAUDE_QUOTA_SOURCE = 'claude-statusline-response'
CLAUDE_NATIVE_QUOTA_SOURCE = 'claude-native-usage'
CODEX_QUOTA_SOURCE = 'codex-native-quota'
QUOTA_WINDOWS = ('five_hour', 'weekly')


def allocation_order(now):
    weekend = datetime.fromtimestamp(now, ZoneInfo('Asia/Seoul')).weekday() >= 5
    if weekend:
        return (('claude','personal'), ('codex','personal'), ('claude','team'), ('codex','team'))
    return (('claude','team'), ('claude','personal'), ('codex','team'), ('codex','personal'))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def eligible(snapshot, now, ttl=120):
    if not isinstance(snapshot, dict) or snapshot.get('unavailable'):
        return False
    at = snapshot.get('observed_at')
    if not number(at) or not 0 <= now - at <= ttl:
        return False
    exempt = snapshot.get('not_applicable', [])
    if 'not_applicable' in snapshot:
        if (snapshot.get('source') != CODEX_QUOTA_SOURCE
                or snapshot.get('ordinary_usage_allowed') is not True
                or not isinstance(exempt, list) or len(exempt) > 1
                or any(not isinstance(key, str) or key not in QUOTA_WINDOWS or key in snapshot for key in exempt)):
            return False
    if snapshot.get('source') == CODEX_QUOTA_SOURCE and snapshot.get('ordinary_usage_allowed') is False:
        return False
    for key in QUOTA_WINDOWS:
        if key in exempt:
            continue
        window = snapshot.get(key)
        if not isinstance(window, dict):
            return False
        used, reset = window.get('used'), window.get('resets_at')
        if not number(used) or not 0 <= used < 90:
            return False
        if snapshot.get('source') != CLAUDE_NATIVE_QUOTA_SOURCE and (not number(reset) or reset <= now):
            return False
    return True


def account_order(config, now):
    if 'account_selection' not in config:
        return ('team', 'personal')
    schedule = config['account_selection']
    if not isinstance(schedule, dict) or set(schedule) != {'timezone', 'weekdays', 'weekends'}:
        raise ValueError('account_selection requires timezone, weekdays and weekends')
    for key in ('weekdays', 'weekends'):
        order = schedule[key]
        if not isinstance(order, list) or len(order) != 2 or any(a not in ('team', 'personal') for a in order) or set(order) != {'team', 'personal'}:
            raise ValueError('account order must contain team and personal once each')
    try:
        zone = ZoneInfo(schedule['timezone'])
    except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError('account_selection requires a valid timezone') from error
    key = 'weekends' if datetime.fromtimestamp(now, zone).weekday() >= 5 else 'weekdays'
    return tuple(schedule[key])


def choose_account(team, personal, now, ttl=120, active=None, *, order=('team', 'personal')):
    if active in ('team', 'personal'):
        return active
    snapshots = {'team': team, 'personal': personal}
    for account in order:
        snapshot = snapshots[account]
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
        # This boundary accepts the authenticated provider response; profile
        # identity/binding checks remain the caller's responsibility.
        result['source'] = CODEX_QUOTA_SOURCE
        raw = raw if isinstance(raw, dict) else {}
        rates = raw.get('rateLimitsByLimitId')
        if rates is None or rates == {}:
            bucket = raw.get('rateLimits')
        else:
            bucket = rates.get('codex') if isinstance(rates, dict) else None
        allowed = raw.get('ordinaryUsageAllowed')
        if type(allowed) is bool:
            result['ordinary_usage_allowed'] = allowed
        # Included usage can remain permitted after the credit spend cap is reached.
        # Only explicit native allowance distinguishes that case from an unknown block.
        if allowed is False or any(
                data.get('rateLimitReachedType') is not None
                or (data.get('spendControlReached') is True and allowed is not True)
                for data in (raw, bucket) if isinstance(data, dict)):
            result['unavailable'] = True
        valid_bucket = isinstance(bucket, dict)
        if isinstance(bucket, dict):
            for window in (bucket.get('primary'), bucket.get('secondary')):
                if window is None:
                    continue
                if not isinstance(window, dict):
                    valid_bucket = False
                    continue
                duration = window.get('windowDurationMins')
                key = {300: 'five_hour', 10080: 'weekly'}.get(duration) if type(duration) is int else None
                if key is None:
                    valid_bucket = False
                    continue
                if key in result:
                    valid_bucket = False
                    result['unavailable'] = True
                    continue
                used, reset = window.get('usedPercent'), window.get('resetsAt')
                result[key] = {'used': used, 'resets_at': reset}
                if not number(used) or not 0 <= used <= 100 or not number(reset) or reset <= now:
                    valid_bucket = False
        if allowed is True and valid_bucket and any(key in result for key in QUOTA_WINDOWS):
            missing = [key for key in QUOTA_WINDOWS if key not in result]
            if missing:
                result['not_applicable'] = missing
    else:
        raise ValueError('unsupported provider')
    return result


def context_action(used, safe_boundary):
    if not number(used) or not 0 <= used <= 100:
        return 'unknown'
    if used >= 90 and safe_boundary:
        return 'handoff'
    return 'prepare' if used >= 80 else 'keep'
