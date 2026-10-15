#!/usr/bin/env python3
"""Local IR source extraction, versioned analysis cards, and incremental refresh."""
import argparse
import codecs
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import ir_language

SKILL = Path(__file__).resolve().parents[1]
SUPPORTED = {'.md', '.txt', '.pptx', '.xlsx', '.pdf'}
VERSION = 1
NS = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      's': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def norm(text):
    return unicodedata.normalize('NFC', str(text)).casefold()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def print_json(data, stream):
    encoding = stream.encoding or 'ascii'
    ascii_output = codecs.lookup(encoding).name != 'utf-8'
    text = json.dumps(data, ensure_ascii=ascii_output, indent=2)
    try:
        text.encode(encoding, errors=stream.errors or 'strict')
    except UnicodeEncodeError:
        text = json.dumps(data, ensure_ascii=True, indent=2)
    print(text, file=stream)


def default_state(root):
    configured = os.environ.get('XDG_STATE_HOME', '')
    base = Path(configured).expanduser() if configured else Path.home() / '.local' / 'state'
    if not base.is_absolute():
        base = Path.home() / '.local' / 'state'
    library_id = hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:16]
    return base / 'ir-references' / library_id


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def rels(z, part):
    relpart = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
    if relpart not in z.namelist():
        return {}
    result = {}
    for el in ET.fromstring(z.read(relpart)):
        target = el.get('Target', '')
        external = el.get('TargetMode') == 'External'
        if not external:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(part), target)).lstrip('/')
        result[el.get('Id')] = {'target': target, 'type': el.get('Type', '').rsplit('/', 1)[-1], 'external': external}
    return result


def paragraphs(root):
    return '\n'.join(''.join(p.itertext()) for p in root.findall('.//a:p', NS)).strip()


def pptx_units(path):
    units = []
    with zipfile.ZipFile(path) as z:
        presentation = ET.fromstring(z.read('ppt/presentation.xml'))
        links = rels(z, 'ppt/presentation.xml')
        for index, slide_id in enumerate(presentation.findall('p:sldIdLst/p:sldId', NS), 1):
            part = links[slide_id.get('{%s}id' % NS['r'])]['target']
            root = ET.fromstring(z.read(part))
            slide_links = rels(z, part)
            notes = []
            for link in slide_links.values():
                if link['type'] == 'notesSlide' and not link['external']:
                    notes_root = ET.fromstring(z.read(link['target']))
                    for shape in notes_root.findall('.//p:sp', NS):
                        ph = shape.find('p:nvSpPr/p:nvPr/p:ph', NS)
                        if ph is None or ph.get('type') not in {'sldNum', 'dt', 'hdr', 'ftr'}:
                            notes.append(paragraphs(shape))
            images = []
            for blip in root.findall('.//a:blip', NS):
                link = slide_links.get(blip.get('{%s}embed' % NS['r']))
                if link and not link['external']:
                    images.append(link['target'])
            text = paragraphs(root)
            note_text = '\n'.join(n for n in notes if n)
            units.append({'locator': f'slide {index}', 'slide': index, 'slide_text': text,
                          'notes': note_text, 'text': text + '\n[발표자 노트]\n' + note_text,
                          'images': list(dict.fromkeys(images)), 'image_only': not bool(text.strip())})
    return units


def xlsx_units(path):
    units = []
    with zipfile.ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(si.itertext()) for si in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        workbook = ET.fromstring(z.read('xl/workbook.xml'))
        links = rels(z, 'xl/workbook.xml')
        for sheet in workbook.findall('s:sheets/s:sheet', NS):
            part = links[sheet.get('{%s}id' % NS['r'])]['target']
            root = ET.fromstring(z.read(part))
            sheet_links = rels(z, part)
            hyperlinks = {}
            for h in root.findall('.//s:hyperlink', NS):
                link = sheet_links.get(h.get('{%s}id' % NS['r']))
                hyperlinks[h.get('ref')] = link['target'] if link else h.get('location', '')
            rows = []
            for row in root.findall('s:sheetData/s:row', NS):
                cells = []
                for cell in row.findall('s:c', NS):
                    address = cell.get('r', '?')
                    value_node = cell.find('s:v', NS)
                    value = (value_node.text or '') if value_node is not None else ''
                    if cell.get('t') == 'inlineStr':
                        value = ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                    elif cell.get('t') == 's' and value:
                        value = strings[int(value)]
                    formula = cell.find('s:f', NS)
                    if formula is not None:
                        value += f' [formula: {formula.text}; cached value above, not recalculated]'
                    if address in hyperlinks:
                        value += ' ' + hyperlinks[address]
                    if value.strip():
                        cells.append(f'{address}: {value}')
                if cells:
                    rows.append({'locator': f"sheet {sheet.get('name')} row {row.get('r')}",
                                 'sheet': sheet.get('name'), 'row': int(row.get('r')), 'text': ' | '.join(cells)})
            context = '\n'.join(r['text'] for r in rows[:5])
            units.extend(dict(row, sheet_context=context) for row in rows)
    return units


def extract(path):
    suffix = path.suffix.lower()
    if suffix == '.pptx':
        return pptx_units(path)
    if suffix == '.xlsx':
        return xlsx_units(path)
    if suffix == '.pdf':
        executable = shutil.which('pdftotext')
        if not executable:
            raise ValueError('pdftotext is required for PDF text extraction; inspect/render with the PDF skill.')
        result = subprocess.run([executable, '-layout', '-enc', 'UTF-8', str(path), '-'], capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError(result.stderr.decode('utf-8', errors='replace')[:400])
        pages = result.stdout.decode('utf-8', errors='replace').split('\f')
        if pages and not pages[-1].strip():
            pages.pop()
        return [{'locator': f'page {i}', 'page': i, 'text': text.strip(), 'image_only': not bool(text.strip())}
                for i, text in enumerate(pages, 1)]
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    units, start, heading = [], 0, ''
    for i in range(len(lines) + 1):
        boundary = i == len(lines) or (i > start and (lines[i].startswith('#') or i - start >= 24))
        if boundary:
            text = '\n'.join(lines[start:i]).strip()
            if text:
                units.append({'locator': f'lines {start + 1}-{i}', 'line': start + 1, 'heading': heading, 'text': text})
            start = i
        if i < len(lines) and lines[i].startswith('#'):
            heading = lines[i].lstrip('# ').strip()
    return units


def sync(root, state, force=False):
    if not root.is_dir():
        raise ValueError(f'Source folder unavailable: {root}. Existing catalog was preserved.')
    cache = state / 'catalog.json'
    old = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
    if old.get('root') != str(root) or old.get('version') != VERSION:
        old = {}
    previous = old.get('sources', {})
    sources, ignored, changed = {}, [], []
    def fail(error):
        raise error
    for directory, dirs, files in os.walk(root, onerror=fail):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(directory) / d).is_symlink())
        for filename in sorted(files):
            path = Path(directory) / filename
            if filename.startswith(('.', '~$')) or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if path.suffix.lower() not in SUPPORTED:
                ignored.append(relative)
                continue
            fingerprint = digest(path)
            before = previous.get(relative, {})
            if before.get('sha256') == fingerprint and not before.get('error') and not force:
                sources[relative] = before
                continue
            record = {'path': relative, 'sha256': fingerprint, 'kind': path.suffix.lower()[1:],
                      'modified_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')}
            try:
                record['units'] = extract(path)
            except (OSError, ValueError, KeyError, IndexError, ET.ParseError, zipfile.BadZipFile, subprocess.TimeoutExpired) as e:
                record['units'] = []
                record['error'] = str(e)
            if digest(path) != fingerprint:
                raise ValueError(f'Source changed during extraction: {relative}. Retry after saving finishes.')
            sources[relative] = record
            changed.append(relative)
    deleted = sorted(set(previous) - set(sources))
    catalog = {'version': VERSION, 'root': str(root), 'synced_at': now(), 'sources': sources, 'ignored': ignored}
    atomic_json(cache, catalog)
    return catalog, {'changed': changed, 'deleted': deleted}


def read_cards(state):
    return [json.loads(p.read_text(encoding='utf-8')) for p in sorted((state / 'analyses').glob('*.json'))]


def card_status(card, catalog):
    for path, fingerprint in card['source_hashes'].items():
        source = catalog['sources'].get(path)
        if source is None:
            return 'source_missing'
        if source.get('error') or source['sha256'] != fingerprint:
            return 'needs_review'
    return 'current'


def pending(catalog, cards, language=()):
    result, covered = [], set()
    for card in cards:
        if card['source'] not in catalog['sources']:
            continue
        covered.add(card['source'])
        status = card_status(card, catalog)
        if status != 'current':
            result.append({'id': card['id'], 'source': card['source'], 'status': status})
        if catalog['sources'][card['source']]['kind'] == 'pptx':
            profile = next((p for p in language if p['id'] == card['id'] and p['source'] == card['source']), None)
            language_state = ir_language.status(profile, catalog) if profile else 'not_analyzed'
            if language_state != 'current':
                result.append({'id': card['id'], 'source': card['source'],
                               'status': 'needs_language_analysis', 'reason': language_state})
    for path, source in catalog['sources'].items():
        if path not in covered:
            result.append({'source': path, 'status': 'extraction_error' if source.get('error') else 'not_analyzed'})
    return result


def record_analysis(data, catalog, state):
    required = ['id', 'title', 'source', 'summary', 'flow', 'strengths', 'strategy', 'application',
                'script_use', 'limitations', 'evidence', 'tags', 'review_scope', 'source_hashes']
    for field in required:
        if not data.get(field):
            raise ValueError(f'Analysis field is required: {field}')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', data['id']):
        raise ValueError('Analysis id must use letters, digits, underscore, or hyphen.')
    if data['source'] not in data['source_hashes']:
        raise ValueError('source_hashes must include the primary source and every inspected dependency.')
    if card_status(data, catalog) != 'current':
        raise ValueError('Analysis source hashes do not match current files. Re-read changed sources before saving.')
    for proof in data['evidence']:
        if proof['source'] not in data['source_hashes']:
            raise ValueError('Every evidence source must be fingerprinted.')
        if not proof.get('locator') or not proof.get('observation'):
            raise ValueError('Evidence needs a locator and an observation.')
    if catalog['sources'][data['source']]['kind'] == 'pptx':
        review = data.get('script_analysis', {})
        for field in ['matched_slides', 'screen_voice_relation', 'effective_connection', 'improvement']:
            if not review.get(field):
                raise ValueError(f'PPTX analysis needs script_analysis.{field}. Pair screen evidence with notes before recording.')
        count = len(catalog['sources'][data['source']]['units'])
        if any(not isinstance(n, int) or n < 1 or n > count for n in review['matched_slides']):
            raise ValueError('Script analysis slide number is outside this deck.')
    elif 'script_analysis' in data:
        raise ValueError('script_analysis is only supported for PPTX sources; omit it for other formats.')
    data['analyzed_at'] = now()
    atomic_json(state / 'analyses' / (data['id'] + '.json'), data)
    return {'saved': data['id'], 'analyzed_at': data['analyzed_at']}


def match_score(query, text, any_term=False):
    terms = [norm(t) for t in query.split()]
    text = norm(text)
    matches = [term in text for term in terms]
    if not terms:
        return 1
    if not (any(matches) if any_term else all(matches)):
        return 0
    return sum(min(text.count(term), 10) for term in terms)


def find_source(value, catalog):
    sources = catalog['sources']
    if value in sources:
        return sources[value]
    matches = [source for path, source in sources.items() if norm(value) in norm(path)]
    if len(matches) != 1:
        raise ValueError(f'Expected one source; found {len(matches)}. Use its full relative path.')
    return matches[0]


def report(catalog, cards, state):
    cards = [card for card in cards if card['source'] in catalog['sources']]
    decks = [card for card in cards if catalog['sources'][card['source']]['kind'] == 'pptx']
    slide_count = sum(len(catalog['sources'][card['source']]['units']) for card in decks)
    pair_count = sum(len(card.get('script_analysis', {}).get('matched_slides', [])) for card in decks)
    lines = ['# IR 레퍼런스 사전 분석', '', f"갱신 확인: {catalog['synced_at']}", '',
             '[회사별 어투 분석](tone-report.md) · [원문 표현집](expression-bank.md)', '',
             f'참고 PPTX {len(decks)}개 · 장표와 발표자 노트 {slide_count}쌍 · 핵심 {pair_count}쌍의 상세 매칭 분석', '',
             '**이 보고서에서 스크립트는 PPTX의 발표자 노트다.** 회사별 전체 흐름은 기존 분석과 자료 구조를 바탕으로 정리하고, 핵심 장표는 화면과 노트를 직접 대조했다. 개별 검토 범위는 각 카드에 기록했다.', '',
             '각 카드는 원본·기존 분석에 근거한 해석이다. 발표 당시 수치를 현재 사실로 재사용하지 않는다.', '',
             '| ID | 자료 | 핵심 전략 | 상태 |', '|---|---|---|---|']
    for card in cards:
        lines.append(f"| {card['id']} | [{card['title']}](#{card['id'].lower()}) | {card['summary']} | {card_status(card, catalog)} |")
    for card in cards:
        lines += ['', f"## {card['id']}", '', f"### {card['title']}", '',
                  f"출처: [{Path(card['source']).name}](<{catalog['root']}/{card['source']}>)", '',
                  f"분석 범위: {card['review_scope']}", '', f"상태: {card_status(card, catalog)} · 분석일: {card['analyzed_at']}"]
        for field, label in [('flow','발표 흐름'),('strengths','장점과 이유'),('strategy','설득 전략'),
                             ('application','적용할 상황'),('script_use','스크립트에 활용'),('limitations','한계와 개선점')]:
            lines += ['', f'#### {label}', '']
            value = card[field]
            lines += ['- ' + item for item in value] if isinstance(value, list) else [value]
        if catalog['sources'][card['source']]['kind'] == 'pptx' and card.get('script_analysis'):
            review = card['script_analysis']
            lines += ['', '#### 장표와 스크립트 매칭 분석', '',
                      '검토 장표: ' + ', '.join('p' + str(n) for n in review['matched_slides']), '',
                      '**화면과 말의 역할:** ' + review['screen_voice_relation'], '',
                      '**전개의 연결:** ' + review['effective_connection'], '',
                      '**스크립트 개선점:** ' + review['improvement']]
            if card_status(card, catalog) == 'current':
                source = catalog['sources'][card['source']]
                for n in review['matched_slides']:
                    unit = source['units'][n-1]
                    lines += ['', f'**p{n}의 제공 발표자 노트**', '', note_body(unit['notes'])]
        lines += ['', '#### 근거', '']
        for proof in card['evidence']:
            lines.append(f"- [{proof['source']}](<{catalog['root']}/{proof['source']}>) · {proof['locator']}: {proof['observation']}")
    destination = state / 'analysis-report.md'
    destination.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'report': str(destination), 'cards': len(cards)}


def note_body(notes):
    return ir_language.note_body(notes)


def reference_urls(notes):
    closing = {')': '(', ']': '[', '}': '{'}
    urls = []
    for candidate in re.findall(r'''https?://[^\s<>"']+''', notes, re.IGNORECASE):
        url = candidate.rstrip('.,;:!?')
        while url and url[-1] in closing and url.count(url[-1]) > url.count(closing[url[-1]]):
            url = url[:-1].rstrip('.,;:!?')
        urls.append(url)
    return urls


def script_pairs(card, source, catalog):
    if source['kind'] != 'pptx':
        raise ValueError('Slide-note pairs are only supported for PPTX sources.')
    reviewed = set(card.get('script_analysis', {}).get('matched_slides', []))
    current = card_status(card, catalog) == 'current'
    for unit in source['units']:
        n = unit.get('slide')
        if n is None:
            continue
        inspected = n in card.get('inspected_slides', []) and current
        observations = [e['observation'] for e in card['evidence']
                        if e['source'] == source['path'] and e['locator'] == f'slide {n}'] if current else []
        yield {'slide': n, 'screen_text': unit['slide_text'], 'screen_observations': observations,
               'screen_basis': '직접 확인한 이미지' if inspected else ('추출된 슬라이드 텍스트' if unit['slide_text'] else '이미지 장표: 개별 시각 검토 필요'),
               'speaker_notes': unit['notes'], 'script_body': note_body(unit['notes']),
               'script_type': '초안으로 표시된 발표자 노트' if unit['notes'].lstrip().startswith('발표문 초안') else '제공 발표자 노트: 원고·근거 메모를 구분해 사용',
               'pairing_basis': 'PPTX의 실제 슬라이드 순서와 notesSlide 관계로 연결',
               'reviewed_pair': n in reviewed and current,
               'analysis': card.get('script_analysis') if n in reviewed and current else None,
               'reference_urls': reference_urls(unit['notes']),
               'matching_limit': '외부 링크는 참고 위치다. 해당 내용과 이 노트의 일치 여부를 뜻하지 않는다.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Folder containing your reference files; scanned recursively.')
    parser.add_argument('--state', type=Path, help='Derived data directory outside the source and skill folders. Default: XDG_STATE_HOME/ir-references/<root-hash>.')
    sub = parser.add_subparsers(dest='command', required=True)
    sync_parser = sub.add_parser('sync')
    sync_parser.add_argument('--force', action='store_true')
    sub.add_parser('pending')
    sub.add_parser('stats')
    sub.add_parser('report')
    for name in ['search', 'sources', 'phrases']:
        p = sub.add_parser(name)
        p.add_argument('query', nargs='?', default='')
        p.add_argument('--any', action='store_true')
        p.add_argument('--limit', type=int, default=8)
        if name == 'phrases':
            p.add_argument('--id', help='Filter by deck analysis id, e.g. EXAMPLE')
            p.add_argument('--role', help='Filter by expression role, e.g. opening question')
    p = sub.add_parser('tone')
    p.add_argument('id')
    p = sub.add_parser('show')
    p.add_argument('id')
    p = sub.add_parser('pairs')
    p.add_argument('id')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--limit', type=int, default=3)
    p.add_argument('--slide', type=int, action='append')
    p = sub.add_parser('source')
    p.add_argument('path')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--limit', type=int, default=3)
    p.add_argument('--slide', type=int, action='append')
    p = sub.add_parser('images')
    p.add_argument('path')
    p.add_argument('--slide', type=int, required=True)
    p = sub.add_parser('record')
    p.add_argument('json_file', type=Path)
    p = sub.add_parser('record-language')
    p.add_argument('json_file', type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    state = (args.state.expanduser() if args.state else default_state(root)).resolve()
    if state == root or root in state.parents:
        raise ValueError('Derived state must be outside the source folder to avoid indexing generated reports.')
    if state == SKILL or SKILL in state.parents:
        raise ValueError('Derived state must be outside the skill folder to keep reference content out of the skill source.')
    catalog, changes = sync(root, state, getattr(args, 'force', False))
    cards = read_cards(state)
    language = ir_language.read_profiles(state)
    active = [card for card in cards if card['source'] in catalog['sources']]
    active_language = [p for p in language if ir_language.status(p, catalog) == 'current']
    work = pending(catalog, cards, language)
    errors = [{'source': p, 'error': s['error']} for p, s in catalog['sources'].items() if s.get('error')]
    result = {'state': str(state), 'pending_count': len(work), 'extraction_errors': errors}
    if (changes['changed'] or changes['deleted']) and (state / 'expression-bank.md').exists():
        ir_language.render_reports(catalog, language, state)
    if args.command in {'stats', 'sync', 'pending'}:
        result.update({'sources': len(catalog['sources']), 'analyses': len(active),
                       'retired_analyses': len(cards) - len(active),
                       'current_analyses': sum(card_status(c, catalog) == 'current' for c in active),
                       'units': sum(len(s['units']) for s in catalog['sources'].values()),
                       'language_profiles': len(active_language),
                       'original_expressions': sum(len(p['expressions']) for p in active_language),
                       'pending': work, 'ignored_files': catalog['ignored'], **changes})
    elif args.command == 'tone':
        profile = next((p for p in language if p['id'].casefold() == args.id.casefold()), None)
        if profile is None:
            raise ValueError('No saved tone analysis; complete record-language for this deck first.')
        result['status'] = ir_language.status(profile, catalog)
        if result['status'] == 'current':
            result['tone_analysis'] = {k: v for k, v in profile.items() if k != 'expressions'}
        else:
            result['message'] = 'Source changed; re-review speaker notes before using this tone analysis.'
    elif args.command == 'phrases':
        ranked = []
        for profile in active_language:
            if args.id and profile['id'].casefold() != args.id.casefold():
                continue
            for expression in profile['expressions']:
                if args.role and norm(args.role) not in norm(expression['role']):
                    continue
                searchable = profile['title'] + ' ' + ' '.join(str(v) for v in expression.values())
                score = match_score(args.query, searchable, args.any)
                if score:
                    ranked.append((score, {'deck_id': profile['id'], 'title': profile['title'],
                                           'source':profile['source'], **expression}))
        ranked.sort(key=lambda item: (-item[0], item[1]['id']))
        result['match_count'] = len(ranked)
        result['matches'] = [item for _, item in ranked[:max(0, args.limit)]]
    elif args.command in {'show', 'pairs'}:
        card = next((c for c in cards if c['id'].casefold() == args.id.casefold()), None)
        if card is None:
            raise ValueError(f'Unknown analysis id: {args.id}')
        if args.command == 'show':
            result.update({'status': card_status(card, catalog), 'analysis': card})
            profile = next((p for p in active_language if p['id'] == card['id']), None)
            if profile:
                result['language_reference'] = {'tone_command': f'tone {card["id"]}',
                                                'expressions_command': f'phrases --id {card["id"]}',
                                                'original_expression_count': len(profile['expressions'])}
        else:
            source = catalog['sources'].get(card['source'])
            if source is None:
                raise ValueError('Source no longer exists; historical card cannot provide current pairs.')
            pairs = list(script_pairs(card, source, catalog))
            selected = [p for p in pairs if p['slide'] in args.slide] if args.slide else pairs[max(0,args.offset):max(0,args.offset)+max(0,args.limit)]
            result.update({'status': card_status(card, catalog), 'source': card['source'], 'sha256':source['sha256'],
                           'pair_count': len(pairs), 'pairs': selected})
    elif args.command == 'search':
        ranked = []
        for card in cards:
            if card_status(card, catalog) != 'current':
                continue
            searchable = ' '.join(str(card[key]) for key in ['title','summary','flow','strengths','strategy','application','script_use','tags'])
            score = match_score(args.query, searchable, args.any)
            if score:
                ranked.append((score, {k: card[k] for k in ['id','title','summary','source','tags']}))
        ranked.sort(key=lambda item: (-item[0], item[1]['id']))
        result['matches'] = [item for score, item in ranked[:max(0, args.limit)]]
    elif args.command == 'sources':
        ranked = []
        for path, source in catalog['sources'].items():
            for unit in source['units']:
                score = match_score(args.query, path + '\n' + unit.get('heading', '') + '\n' + unit['text'], args.any)
                if score:
                    text = unit['text']
                    positions = [norm(text).find(norm(t)) for t in args.query.split() if norm(t) in norm(text)]
                    start = max(0, min(positions, default=0) - 100)
                    ranked.append((score, {'source': path, 'locator': unit['locator'], 'excerpt': text[start:start+600]}))
        ranked.sort(key=lambda item: -item[0])
        result['matches'] = [item for score, item in ranked[:max(0, args.limit)]]
    elif args.command in {'source', 'images'}:
        source = find_source(args.path, catalog)
        if source.get('error'):
            raise ValueError(source['error'])
        if args.command == 'source':
            units = source['units']
            selected = [u for u in units if u.get('slide') in args.slide] if args.slide else units[max(0,args.offset):max(0,args.offset)+max(0,args.limit)]
            result.update({'source': source['path'], 'sha256': source['sha256'], 'unit_count': len(units), 'units': selected})
        else:
            unit = next((u for u in source['units'] if u.get('slide') == args.slide), None)
            if unit is None:
                raise ValueError('Slide not found.')
            images = []
            with zipfile.ZipFile(root / source['path']) as z:
                for i, part in enumerate(unit['images'], 1):
                    destination = state / 'images' / source['sha256'][:16] / f"slide-{args.slide}-{i}{Path(part).suffix}"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(z.read(part))
                    images.append(str(destination))
            result.update({'images': images, 'note': 'Embedded images only; render the slide to inspect layouts containing editable objects.'})
    elif args.command == 'record':
        result.update(record_analysis(json.loads(args.json_file.read_text(encoding='utf-8')), catalog, state))
        result['pending_count'] = len(pending(catalog, read_cards(state), language))
    elif args.command == 'record-language':
        data = ir_language.validate(json.loads(args.json_file.read_text(encoding='utf-8')), catalog, cards)
        data['analyzed_at'] = now()
        atomic_json(state / 'language' / (data['id'] + '.json'), data)
        result.update({'saved_language': data['id'], 'original_expressions': len(data['expressions'])})
        result['pending_count'] = len(pending(catalog, cards, ir_language.read_profiles(state)))
    elif args.command == 'report':
        result.update(report(catalog, cards, state))
        result.update(ir_language.render_reports(catalog, language, state))
    print_json(result, sys.stdout)


if __name__ == '__main__':
    # Restrict newly created CLI files without changing existing directory permissions.
    os.umask(0o077)
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print_json({'error': str(error)}, sys.stderr)
        sys.exit(1)
