"""Version-bound user explanations beside an unchanged design source."""
import copy
import re
import unicodedata


_TEXT_FIELDS=('summary','purpose','impact','decision_explanation','current_activity')
_LIST_FIELDS=('changes','checks')
_ESCAPE=re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')


def _text(value):
    if not isinstance(value,str):return ''
    value=_ESCAPE.sub('',value)
    return ''.join(char for char in value if char in '\n\t' or not unicodedata.category(char).startswith('C')).strip()


def _texts(value):
    if not isinstance(value,list) or any(not isinstance(item,str) for item in value):return None
    return [text for item in value if (text:=_text(item))]


def _valid_binding(revision,digest):
    if type(revision) is not int or revision<1 or not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):
        return None
    return revision,digest


def _card_binding(card):
    explicit=any(key in card for key in ('document_revision','document_digest'))
    direct=all(key in card for key in ('revision','digest'))
    primary=_valid_binding(card.get('document_revision'),card.get('document_digest')) if explicit else None
    alternate=_valid_binding(card.get('revision'),card.get('digest')) if direct else None
    if explicit:
        if primary is None or direct and primary!=alternate:return None
        return primary
    return alternate


def _fallback(contract,card):
    requirements=_texts(contract.get('requirements',[])) or []
    paths=_texts(contract.get('allowed_paths',[])) or []
    checks=[text for step in contract.get('steps',[]) if isinstance(step,dict) and (text:=_text(step.get('title')))]
    interaction=card.get('interaction') if isinstance(card.get('interaction'),dict) else {}
    activity=[_text(interaction.get('label') or card.get('status_label')),_text(interaction.get('reason'))]
    question=card.get('question') if isinstance(card.get('question'),dict) else {}
    if card.get('needs_decision') is False and not question:
        decision='현재 사용자 결정 요청은 없습니다.'
    elif card.get('needs_decision') or question:
        decision=_text(question.get('prompt')) or '이 카드의 현재 선택지에서 필요한 결정을 확인해 주세요.'
    else:
        decision='승인 여부와 처리 범위는 카드의 현재 선택지에서 확인해 주세요.'
    return {
        'summary':_text(contract.get('title')) or _text(card.get('title')) or '작업 설명',
        'purpose':requirements[0] if requirements else '요구사항과 완료 조건을 확인해 주세요.',
        'changes':requirements,
        'checks':checks,
        'impact':'변경 허용 범위: '+' · '.join(paths) if paths else '변경 범위 정보가 없습니다. 전체 설계와 계약을 확인해 주세요.',
        'decision_explanation':decision,
        'current_activity':' · '.join(item for item in activity if item) or '현재 작업 상태 정보가 없습니다.',
    }


def reading_view(source_body,contract,card,profile=None):
    """Return a display brief, separately copied contract, and the exact input text.

    Briefs never borrow current status from a plan's historical prose. An authored
    brief is usable only for its exact document revision and content digest.
    """
    if not isinstance(source_body,str):raise TypeError('설계 원문은 디코딩한 문자열이어야 합니다.')
    if not isinstance(contract,dict) or not isinstance(card,dict):raise TypeError('설계 계약과 카드 정보가 필요합니다.')
    separate={key:copy.deepcopy(contract.get(key,[])) for key in ('allowed_paths','requirements','steps')}
    brief=_fallback(contract,card)
    state='missing' if profile is None else 'stale'
    if isinstance(profile,dict):
        document=profile.get('document')
        binding=_card_binding(card)
        if (isinstance(document,dict) and binding is not None
                and _valid_binding(document.get('revision'),document.get('digest'))==binding):
            state='current'
            for key in _TEXT_FIELDS:
                if value:=_text(profile.get(key)):brief[key]=value
            for key in _LIST_FIELDS:
                value=_texts(profile.get(key))
                if value is not None:brief[key]=value
    return {'body':source_body,'source_body':source_body,'contract':separate,'brief':brief,'brief_state':state}
