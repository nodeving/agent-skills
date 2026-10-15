"""Validated, source-linked tone analysis and original expression collections."""
import json
from pathlib import Path
import re
from statistics import mean, median

VERSION = 1
TONE_FIELDS = ('summary', 'sentence_structure', 'register', 'stance', 'audience_address',
               'transitions', 'strength', 'caution', 'adaptation')
EXPRESSION_FIELDS = ('quote', 'role', 'why_good', 'pattern', 'usage', 'caution')


def note_body(notes):
    if notes.lstrip().startswith('발표문 초안'):
        return notes.split('자료:', 1)[0].replace('발표문 초안', '', 1).strip()
    return notes.strip()


def sentences(notes):
    return [s.strip() for s in re.split(r'(?<=[.!?。])\s+|\n+', note_body(notes)) if s.strip()]


def metrics(source):
    notes = [note_body(u.get('notes', '')) for u in source['units'] if u.get('slide')]
    parts = [sentences(n) for n in notes]
    lengths = [len(s) for group in parts for s in group]
    return {'slide_count': len(notes), 'nonempty_notes': sum(bool(n) for n in notes),
            'sentence_count': len(lengths),
            'mean_sentences_per_nonempty_note': round(len(lengths) / max(1, sum(bool(n) for n in notes)), 2),
            'mean_sentence_chars': round(mean(lengths), 1) if lengths else 0,
            'median_sentence_chars': median(lengths) if lengths else 0,
            'question_ending_sentences': sum(bool(re.search(r'(까요|습니까|나요)[.?!]?$|\?$', s)) for g in parts for s in g),
            'method': '공백 뒤의 문장부호 또는 줄바꿈으로 분할. 글자 수는 공백·문장부호 포함. 질문형 종결 검출은 표기 기준이며 음성·속도·억양을 측정하지 않는다.'}


def read_profiles(state):
    return [json.loads(p.read_text(encoding='utf-8')) for p in sorted((state / 'language').glob('*.json'))]


def status(profile, catalog):
    source = catalog['sources'].get(profile['source'])
    if source is None:
        return 'source_missing'
    if source.get('error') or profile['source_sha256'] != source['sha256']:
        return 'needs_review'
    if profile.get('version') != VERSION:
        return 'needs_language_upgrade'
    return 'current'


def validate(data, catalog, cards):
    card = next((c for c in cards if c['id'] == data.get('id')), None)
    if card is None or card['source'] != data.get('source'):
        raise ValueError('Language analysis must match an existing card id and primary source.')
    source = catalog['sources'].get(data['source'])
    if source is None or source.get('error') or source['kind'] != 'pptx':
        raise ValueError('Language analysis needs a readable current PPTX.')
    if data.get('source_sha256') != source['sha256']:
        raise ValueError('Language source hash changed; re-read speaker notes before saving.')
    for field in TONE_FIELDS:
        if not isinstance(data.get('tone', {}).get(field), str) or not data['tone'][field].strip():
            raise ValueError(f'Missing tone.{field}')
    coverage = data.get('coverage', {})
    reviewed = coverage.get('reviewed_note_slides', [])
    available = {u['slide']: u for u in source['units']}
    if not reviewed or len(set(reviewed)) != len(reviewed) or any(n not in available for n in reviewed):
        raise ValueError('coverage.reviewed_note_slides must list valid, unique reviewed slides.')
    if not coverage.get('scope') or not data.get('selection_note'):
        raise ValueError('Document the review scope and expression selection criteria.')
    evidence = data.get('tone_evidence', [])
    for field in TONE_FIELDS:
        if field not in {'caution', 'adaptation'} and not any(field in e.get('supports', []) for e in evidence):
            raise ValueError(f'Tone dimension {field} needs source-note evidence.')
    for item in evidence + data.get('expressions', []):
        slide = item.get('slide')
        quote = item.get('quote', '')
        if slide not in reviewed or not quote.strip() or quote not in note_body(available[slide].get('notes', '')):
            raise ValueError(f'Quote is not verbatim in the reviewed note at slide {slide}: {quote[:60]}')
    expressions = data.get('expressions')
    if not isinstance(expressions, list):
        raise ValueError('expressions must be a list; use [] with selection_note when none are suitable.')
    ids = set()
    for item in expressions:
        if not re.fullmatch(re.escape(data['id']) + r'-E\d+', item.get('id', '')) or item['id'] in ids:
            raise ValueError('Each expression needs a unique id such as EXAMPLE-E01.')
        ids.add(item['id'])
        for field in EXPRESSION_FIELDS:
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f'Missing expression.{field}')
    data['title'] = card['title']
    data['version'] = VERSION
    data['metrics'] = metrics(source)
    return data


def render_reports(catalog, profiles, state):
    active = [p for p in profiles if status(p, catalog) == 'current']
    stale = [p['id'] for p in profiles if p['source'] in catalog['sources'] and status(p, catalog) != 'current']
    covered = {p['source'] for p in active}
    pending_sources = [path for path, source in catalog['sources'].items()
                       if source['kind'] == 'pptx' and path not in covered]
    tone = ['# 발표자 노트의 어투·전환 분석', '', f"원본 확인: {catalog['synced_at']}", '',
            f'어투 분석이 있는 PPTX {len(active)}개 · 노트 {sum(p["metrics"]["nonempty_notes"] for p in active)}개', '',
            '분석 대상은 PPTX에 저장된 발표자 노트다. 문장·종결·청중 지향·확신 수준·전환 방식을 분석했다. 실제 발표자의 억양·속도·성격에 대한 분석은 아니다.', '',
            '문장 통계는 전체 노트에서 자동 계산하고, 어투 해석과 표현 선정은 원문을 읽고 작성했다. 아래 응용 방법은 원문 인용과 구분한다.', '']
    bank = ['# 발표 스크립트 원문 표현집', '', f"원본 확인: {catalog['synced_at']}", '',
            f'현재 PPTX {len(active)}개의 원문 표현 {sum(len(p["expressions"]) for p in active)}개', '',
            '따옴표 안은 발표자 노트의 원문이다. 문장 틀은 새 원고에 적용하기 위한 해석이며 원문의 발화가 아니다. 출처 장표의 앞뒤 문맥과 실제 회사 사실에 맞춰 사용한다.', '',
            '전체 순위나 투자 유치 효과를 평가한 목록은 아니다. 각 표현의 구체적 기능과 재사용 조건을 기준으로 골랐다.', '']
    if stale:
        message = '원본 변경으로 재검토 중인 자료는 제외했다: ' + ', '.join(stale)
        tone += [message, '']
        bank += [message, '']
    if pending_sources:
        message = f'어투·표현 분석을 추가하거나 갱신해야 하는 PPTX: {len(pending_sources)}개.'
        tone += [message, '']
        bank += [message, '']
    tone += ['| 자료 | 어투 요약 | 검토 노트 | 문장당 평균 글자 |', '|---|---|---:|---:|']
    for p in active:
        tone.append(f'| [{p["id"]} {p["title"]}](#{p["id"].lower()}) | {p["tone"]["summary"]} | {len(p["coverage"]["reviewed_note_slides"])} | {p["metrics"]["mean_sentence_chars"]} |')
    labels = {'summary':'전체 어투', 'sentence_structure':'문장 구조', 'register':'격식과 어휘', 'stance':'확신과 계획의 표현',
              'audience_address':'청중에게 말을 거는 방식', 'transitions':'전환과 흐름', 'strength':'참고할 장점',
              'caution':'보완할 점', 'adaptation':'새 원고에 적용'}
    for p in active:
        tone += ['', f'## {p["id"]}', '', f'### {p["title"]}', '',
                 f'출처: [{Path(p["source"]).name}](<{catalog["root"]}/{p["source"]}>)', '',
                 '검토 범위: ' + p['coverage']['scope'], '']
        for field, label in labels.items():
            tone += [f'**{label}:** {p["tone"][field]}', '']
        tone += ['**원문 근거**', '']
        for e in p['tone_evidence']:
            tone += [f'- p{e["slide"]}: “{e["quote"]}”']
    roles = sorted({e['role'] for p in active for e in p['expressions']})
    for role in roles:
        bank += [f'## {role}', '']
        for p in active:
            for e in p['expressions']:
                if e['role'] != role:
                    continue
                bank += [f'### {e["id"]} · {p["title"]} · p{e["slide"]}', '',
                         f'> {e["quote"]}', '', f'출처: [{Path(p["source"]).name}](<{catalog["root"]}/{p["source"]}>) · 발표자 노트 p{e["slide"]}', '',
                         '**좋은 이유:** ' + e['why_good'], '', '**응용할 문장 틀:** ' + e['pattern'], '',
                         '**활용 조건:** ' + e['usage'], '', '**주의할 점:** ' + e['caution'], '']
    destinations = {'tone_report': state / 'tone-report.md', 'expression_report': state / 'expression-bank.md'}
    for key, lines in [('tone_report', tone), ('expression_report', bank)]:
        path = destinations[key]
        temporary = path.with_suffix('.md.tmp')
        temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        temporary.replace(path)
    return {**{key: str(path) for key, path in destinations.items()}, 'language_profiles':len(active),
            'expressions':sum(len(p['expressions']) for p in active)}
