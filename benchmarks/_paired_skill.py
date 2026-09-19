"""One isolated skill used only by the explicitly selected skill experiment."""
import hashlib
from pathlib import Path

SKILL_NAME = 'grepglint-explore'
SKILL_SOURCE = Path(__file__).resolve().parent / 'skills' / SKILL_NAME / 'SKILL.md'
SKILL_RELATIVE = 'codex/skills/' + SKILL_NAME + '/SKILL.md'
# Observed from the pinned client's first offline request, with runtime paths normalized.
SKILL_CATALOG_SHA256 = '24d345152abeca34f8f7d557fb2c88e9548fcb8c499414c4772ef3153726d45c'
SKILL_READ_ENTRY = '<entry access="read"><path><temporary>/codex/skills</path></entry>'


def skill_text():
    with SKILL_SOURCE.open('rb') as stream:
        data = stream.read(16 * 1024 + 1)
    if len(data) > 16 * 1024:
        raise ValueError('skill_content_limit_exceeded')
    return data.decode('utf-8')


def skill_hash():
    return hashlib.sha256(skill_text().encode()).hexdigest()


def skill_enabled(trial):
    return trial.get('guidance') == 'skill-v1' and trial['configuration'] == 'grepglint'


def inspect_skill_request(normalized_request, enabled):
    """Verify the sole skill catalog and remove only its declared comparison differences."""
    import copy
    from _codex_audit import encoded
    from _codex_capture import ProbeError
    from codex_preflight import digest, instruction_blocks
    request = copy.deepcopy(normalized_request)
    catalogs, permissions = 0, 0
    for item in request.get('input', []):
        if item.get('role') not in ('system', 'developer', 'user'):
            continue
        content = item.get('content', [])
        plain = isinstance(content, str)
        parts = [{'text': content}] if plain else content
        kept = []
        for part in parts:
            text = part.get('text', '')
            if '<skills_instructions>' in text:
                if item.get('role') != 'developer' or digest(text) != SKILL_CATALOG_SHA256:
                    raise ProbeError('unexpected_skill_catalog')
                catalogs += 1
                continue
            if SKILL_READ_ENTRY in text:
                if item.get('role') != 'user' or not text.startswith('<environment_context>'):
                    raise ProbeError('unexpected_skill_permission_context')
                permissions += text.count(SKILL_READ_ENTRY)
                part['text'] = text.replace(SKILL_READ_ENTRY, '')
            kept.append(part)
        item['content'] = (kept[0]['text'] if kept else '') if plain else kept
    if catalogs != int(enabled) or permissions != int(enabled):
        raise ProbeError('missing_or_unexpected_skill_catalog_or_permission')
    # Empty content parts left by catalog removal produce no instruction block.
    request['input'] = [item for item in request.get('input', []) if item.get('content') != '']
    blocks = [{k: v for k, v in block.items() if k != 'location'} for block in instruction_blocks(request)]
    return {'catalog_sha256': SKILL_CATALOG_SHA256 if enabled else None,
            'contents_sha256': skill_hash() if enabled else None,
            'comparison_instruction_sha256': digest(encoded(blocks))}
