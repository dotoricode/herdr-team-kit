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
        if re.fullmatch(r'[─━═-]{3,}',line.strip()):
            return not any(value.strip() for value in rest[:index])
    while rest and not rest[-1].strip():rest.pop()
    if not rest:return True
    # A native unframed footer is a final one/two-row block separated from
    # the composer by a blank row. Any preceding content remains a draft.
    footer=len(rest)-1
    while footer and rest[footer-1].strip():footer-=1
    return footer>0 and len(rest)-footer<=2 and not any(value.strip() for value in rest[:footer])
