"""Stable display aliases; never use a name as routing or identity evidence."""
import hashlib


PROFILE_NAMES = {'claude/team': '김척척', 'claude/personal': '박번뜩',
                 'codex/team': '이뚝딱', 'codex/personal': '최꼼꼼'}
WORKER_SURNAMES = ('문', '백', '남', '노', '류', '홍', '배', '송', '안', '양', '우', '유',
                   '전', '차', '채', '하', '허', '곽', '구', '나', '도', '마', '변', '석',
                   '성', '소', '손', '심', '여', '엄', '원', '진')

WORKER_NAMES = ('새봄', '푸름', '다온', '가온', '나래', '누리', '다솜', '도담',
                '라온', '마루', '바름', '보람', '서로', '소담', '수리', '슬기',
                '아람', '여울', '온유', '윤슬', '자람', '차오', '초롱', '하람',
                '하루', '해솔', '해온', '빛결', '가람', '이든', '한결', '솔찬')
COMMON_NAMES = {'coordinator': '도차곡', 'ui': '유깐깐', 'documenter': '송술술', 'operations': '민쫀쫀'}


def display_name(provider, account, *, product=None, role=None, owner=None, aliases=None):
    profile_key = provider + '/' + account
    aliases = aliases if isinstance(aliases, dict) else {}
    product_aliases = aliases.get(product, {})
    configured = product_aliases.get(profile_key) if isinstance(product_aliases, dict) else None
    role_aliases = aliases.get('_roles', {}).get(role, {}) if isinstance(aliases.get('_roles', {}), dict) else {}
    role_name = role_aliases.get(profile_key) if isinstance(role_aliases, dict) else None
    if owner is None and isinstance(role_name, str) and role_name.strip():
        name = role_name.strip()
    elif owner is None and role in COMMON_NAMES:
        name = COMMON_NAMES[role]
    elif owner is None and isinstance(configured, str) and configured.strip():
        name = configured.strip()
    elif owner is None and profile_key in PROFILE_NAMES:
        name = PROFILE_NAMES[profile_key]
    elif provider in ('claude', 'codex'):
        # A ticket worker is separate from a persistent pool employee.
        key = str(owner or product or role or provider)
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], 'big')
        name = WORKER_SURNAMES[number % len(WORKER_SURNAMES)] + WORKER_NAMES[(number // len(WORKER_SURNAMES)) % len(WORKER_NAMES)]
    else:
        name = '직원'
    account_label = {'personal': '개인', 'team': '팀'}.get(account, '계정 미확인')
    return f'{name} ({account_label})'
