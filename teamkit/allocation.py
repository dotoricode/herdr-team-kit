"""Optional usage-based allocation, independent of role/model routing."""
import time

from .policy import account_order


def usage_only(config):
    value = config.get('operations', {}).get('allocation_policy', 'role')
    if value not in ('role', 'usage'):
        raise ValueError('operations.allocation_policy must be role or usage')
    return value == 'usage'


def profile_candidates(config):
    return [dict(provider=provider, account=account, model=pr['model'],
                 effort=pr.get('effort', 'medium'))
            for account in account_order(config, time.time())
            for provider in ('claude', 'codex') if provider in config['profiles']
            for pr in [config['profiles'][provider][account]]]


def refresh_usage(config_path, state):
    """Refresh stale quota once per account/minute from the automatic dispatcher.

    Read-only candidate previews never call this function. Save the retry time
    before a probe so a failed external read cannot create a tight retry loop.
    """
    import json
    import time
    from pathlib import Path
    from .config import load, cached_quota, profile
    from .context_handoff import locked
    from .policy import number
    from .store import Store
    config = load(config_path)
    if not usage_only(config):
        return None
    path = Path(state).parent / 'allocation-usage-refresh.json'
    with locked(Path(state).parent / 'allocation-usage-refresh'):
        previous = json.loads(path.read_text()) if path.exists() else {}
        result = dict(previous)
        for candidate in profile_candidates(config):
            provider, account = candidate['provider'], candidate['account']
            key = provider + '/' + account
            store = Store(state, read_only=True)
            try:
                snapshot = cached_quota(config, store, provider, account)
            finally:
                store.close()
            now = time.time()
            at = (snapshot or {}).get('observed_at')
            if number(at) and 0 <= now-at <= config['quota_ttl_seconds']:
                continue
            if 0 <= now-result.get(key, {}).get('attempted_at', 0) < 60:
                continue
            result[key] = {'attempted_at': now, 'status': 'checking'}
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result)); temporary.replace(path)
            try:
                if provider == 'claude':
                    from .app_usage import refresh_native_usage
                    refresh_native_usage(config, state, account)
                else:
                    from .monitor import collect_and_save
                    collect_and_save(state, config_path, account, profile(config, provider, account))
                result[key]['status'] = 'checked'
            except Exception as error:
                result[key].update(status='unavailable', reason=str(error))
            temporary.write_text(json.dumps(result)); temporary.replace(path)
        return result
