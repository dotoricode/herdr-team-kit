"""Recognize a visibly empty native input without accepting multiline drafts."""
import re


def empty_input(text, registered_hint=None):
    prompts=list(re.finditer(r'^[ \t]*([›❯])[^\S\r\n]*(.*)$',text,re.M))
    if not prompts:return False
    match=prompts[-1]
    value=match.group(2).strip()
    if value not in ('','Ask Codex to do anything',registered_hint):return False
    following=next((line for line in text[match.end():].splitlines() if line.strip()),'')
    if not following:return True
    if re.fullmatch(r'[─━═-]{3,}',following):return True
    # Codex has an unframed footer; require its actual percentage/status shape.
    if match.group(1)!='›':return False
    footer=r'Context (?:100|[0-9]{1,2})% left(?:\s*·.*)?'
    short=re.fullmatch(r'\s*'+footer+r'\s*',following)
    detailed=re.fullmatch(r'\s*GPT-[A-Za-z0-9.-]+[^·\r\n]{0,80}·\s*(?:~|/)[^\r\n]{1,240}·\s*'+footer+r'\s*',following)
    if not (short or detailed):return False
    rest=text[match.end():].splitlines();first=next(i for i,line in enumerate(rest) if line.strip())
    return all(not line.strip() or re.fullmatch(r'\s*(?:[←?/].*for (?:agents|shortcuts).*|⚠[^\r\n]{0,160})',line) for line in rest[first+1:])
