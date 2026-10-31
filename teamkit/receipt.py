"""Fresh host identity checks for sandboxed Codex receipt requests.

The ledger remains a local attestation, not a cryptographic authentication service.
No credentials are copied and no worker write permissions are expanded.
"""
from concurrent.futures import ThreadPoolExecutor
import time
import uuid

from .config import binding, load, profile
from . import providers

PROTOCOL = 'host-fresh-v1'


def requested(store, task, owner, profile_binding):
    with store.transaction():
        value = store.get(task)
        runtime = value.get('runtime', {})
        if value.get('owner') != owner or value['state'] not in ('submitted', 'uncertain') or runtime.get('receipt_protocol') != PROTOCOL or runtime.get('profile_binding') != profile_binding:
            raise ValueError('receipt request does not match dispatched owner/profile')
        old = runtime.get('receipt_request')
        if old:
            raise ValueError('receipt already requested; inspect instead of resending')
        request = {'nonce':uuid.uuid4().hex, 'owner':owner, 'attempt':value['attempt_count'],
                   'binding':profile_binding, 'expires_at':time.time()+30}
        runtime['receipt_request'] = request
        value['runtime'] = runtime
        store._save(value, 'receipt_requested', request)
        return request


def finish(store, expected, request, error=None):
    with store.transaction():
        value = store.get(expected['id'])
        if value != expected or value.get('owner') != request['owner'] or value['attempt_count'] != request['attempt'] or value['state'] not in ('submitted', 'uncertain') or value.get('runtime', {}).get('receipt_request') != request:
            raise ValueError('receipt changed during host verification; inspect')
        if time.time() >= request['expires_at']:
            error = 'receipt verification deadline expired'
        runtime = value['runtime']
        if error:
            runtime['receipt_error'] = error
            value['state'] = 'uncertain'
            return store._save(value, 'receipt_rejected', {'reason':error})
        value['state'] = 'running'
        runtime.update(identity_checked=True, identity_source='host-fresh-provider',
                       receipt_confirmed=request['nonce'])
        return store._save(value, 'receipted', {'source':'host-fresh-provider'})


def verify_pending(store, config_path, task, owner):
    expected = store.get(task)
    request = expected.get('runtime', {}).get('receipt_request')
    if not request or expected.get('owner') != owner or expected['state'] not in ('submitted', 'uncertain') or expected.get('runtime', {}).get('receipt_error'):
        return
    if time.time() >= request['expires_at']:
        finish(store, expected, request, 'receipt verification deadline expired')
        return
    try:
        config = load(config_path)
        pr = profile(config, expected['provider'], expected['account'])
        if binding(pr) != request['binding'] or binding(pr) != expected['runtime']['profile_binding']:
            raise ValueError('receipt profile configuration changed')
        # No SQLite transaction is held while this fresh provider request runs.
        providers.identity(expected['provider'], pr)
        current = load(config_path)
        if binding(profile(current, expected['provider'], expected['account'])) != request['binding']:
            raise ValueError('receipt profile configuration changed')
    except Exception:
        finish(store, expected, request, 'host identity verification failed; inspect provider/config')
        return
    finish(store, expected, request)


def wait_for_confirmation(store, task, owner, profile_binding):
    request = requested(store, task, owner, profile_binding)
    while True:
        value = store.get(task)
        runtime = value.get('runtime', {})
        if value.get('owner') != owner or value.get('attempt_count') != request['attempt'] or runtime.get('receipt_request') != request:
            raise ValueError('receipt ownership changed while waiting')
        if value['state'] == 'running' and runtime.get('receipt_confirmed') == request['nonce']:
            return value
        if runtime.get('receipt_error'):
            raise ValueError(runtime['receipt_error'])
        if time.time() >= request['expires_at']:
            finish(store, value, request, 'receipt verification deadline expired')
            raise ValueError('receipt verification deadline expired; inspect before retry')
        time.sleep(0.1)


def prompt(runtime, store, config_path, task, owner, text):
    # Only the blocking Herdr call runs in another thread. The SQLite connection
    # and provider verification remain on the dispatching host thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.prompt, task['runtime']['name'], text)
        while not future.done():
            verify_pending(store, config_path, task['id'], owner)
            time.sleep(0.1)
        return future.result()
