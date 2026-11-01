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
    return bool(match.group(1)=='›' and re.fullmatch(r'\s*Context (?:100|[0-9]{1,2})% left(?:\s*·.*)?\s*',following))
