"""Recognize a visibly empty native input without accepting multiline drafts."""
import re


def empty_input(text, registered_hint=None):
    prompts=list(re.finditer(r'^[ \t]*([›❯])[^\S\r\n]*(.*)$',text,re.M))
    if not prompts:return False
    match=prompts[-1]
    value=match.group(2).strip()
    if value not in ('','Ask Codex to do anything',registered_hint):return False
    # Input continuation is adjacent to the prompt. A blank row or frame ends it;
    # footer wording, model names and paths are not part of that contract.
    rest=text[match.end():].splitlines()
    if rest and not rest[0].strip():
        rest=rest[1:]
    for index,line in enumerate(rest):
        value=line.strip()
        if not value or re.fullmatch(r'[─━═-]{3,}',value):
            return True
        # Older unframed terminals put metric rows directly below the input.
        # Recognize metrics without coupling to a CLI/model/version vocabulary.
        if '%' in value and any(char.isdigit() for char in value):
            continue
        if value.startswith(('←','?','/','⚠')) and index:
            continue
        return False
    return True
