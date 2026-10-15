#!/usr/bin/env python3
"""Resolve design documents and UX archives to shared, evidence-bearing entities."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit

from library import file_signature, library_root, local_file, require_library


class InputsChanged(ValueError): pass


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def host(value):
    return (urlsplit(value).hostname or '').casefold().removeprefix('www.').rstrip('.')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.'+path.name+'-', delete=False) as stream:
        temp = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    try: temp.replace(path)
    finally: temp.unlink(missing_ok=True)


@contextmanager
def registry_lock(root):
    (root/'catalog').mkdir(parents=True, exist_ok=True)
    with (root/'catalog/.site-registry.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class Resolver:
    def __init__(self, catalog, aliases):
        self.brands = {b['id']: b for b in catalog['brands']}
        self.names, self.hosts = defaultdict(set), defaultdict(set)
        self.aliases = {key(a['product']): a for a in aliases.get('aliases', [])}
        self.domain_aliases = {a['host']: a for a in aliases.get('domain_aliases', [])}
        for b in catalog['brands']:
            self.names[key(b['name'])].add(b['id'])
            if b.get('homepage'):
                self.hosts[host(b['homepage'])].add(b['id'])
        for d in catalog['documents']:
            for name in (d['brand'], d['source_slug'], *d.get('aliases', [])):
                self.names[key(name)].add(d['brand_id'])
        for domain, alias in self.domain_aliases.items():
            if alias['brand_id'] not in self.brands:
                raise ValueError('Unknown domain alias entity: '+alias['brand_id'])
            self.hosts[domain].add(alias['brand_id'])

    def resolve(self, product, site_url=''):
        domain = host(site_url)
        domain_ids = self.hosts.get(domain, set()) if domain else set()
        if len(domain_ids) == 1:
            if domain in self.domain_aliases:
                alias=self.domain_aliases[domain]
                return {'entity_id':alias['brand_id'],'basis':'reviewed-domain-alias','relation':alias['relation'],
                        'confidence':'reviewed','evidence':alias['evidence'],'note':alias['reason']}
            return {'entity_id': next(iter(domain_ids)), 'basis': 'exact-homepage-host',
                    'relation': 'same-site', 'confidence': 'high', 'evidence': [site_url]}
        alias = self.aliases.get(key(product))
        if alias:
            bid = alias['brand_id']
            if bid not in self.brands:
                raise ValueError('Alias points to unknown brand: ' + bid)
            known = host(self.brands[bid].get('homepage', ''))
            if domain and known and domain != known:
                return {'entity_id': None, 'basis': 'domain-conflict', 'candidates': [bid], 'evidence': [site_url, self.brands[bid]['homepage']]}
            return {'entity_id': bid, 'basis': 'reviewed-source-alias', 'relation': alias['relation'],
                    'confidence': 'reviewed', 'evidence': alias['evidence'], 'note': alias['reason']}
        ids = self.names.get(key(product), set())
        if len(ids) == 1:
            bid = next(iter(ids))
            known = host(self.brands[bid].get('homepage', ''))
            if domain and known and domain != known:
                return {'entity_id': None, 'basis': 'domain-conflict', 'candidates': [bid], 'evidence': [site_url, self.brands[bid]['homepage']]}
            return {'entity_id': bid, 'basis': 'unique-exact-source-name', 'relation': 'same-product',
                    'confidence': 'name-supported-domain-unverified', 'evidence': [product],
                    'note': 'Exact provider name or source slug; not a live-domain verification.'}
        return {'entity_id': None, 'basis': 'ambiguous-name' if ids else 'no-document-match', 'candidates': sorted(ids)}


def ux_cases(root):
    """Group original app snapshots, but never collapse screen or Flow IDs."""
    result, seen = [], set()
    for selection in sorted(root.glob('archives/*/selection.json')):
        data = read(selection)
        for app in data['apps']:
            directory = selection.parent / app['archiveDir']
            metadata = read(directory / 'app.json')
            flows = read(directory / 'all-flows.json')
            if flows['status'] != 'complete':
                raise ValueError('Incomplete app archive: ' + str(directory))
            revision = metadata['version']
            relative = directory.relative_to(root).as_posix()
            ids = [f"app-archive:{revision}:{f['flowId']}" for f in flows['flows']]
            seen.update(ids)
            images = [dict(s, path=relative + '/' + s['path']) for s in flows['screens'][:3]]
            result.append({'id': 'ux:app:' + revision, 'product': app['name'], 'platform': app['platform'],
                           'source': 'app-archive',
                           'source_url': app.get('flowUrl', app['url']), 'site_url': app.get('site_url', ''),
                           'kind': 'app-archive', 'record_ids': ids, 'flow_count': len(ids), 'screen_count': len(flows['screens']),
                           'screens': images, 'captured_label': metadata.get('captureLabel', ''),
                           'collected_at': metadata.get('savedAt', metadata.get('capturedAt', data['createdAt'])),
                           'local_metadata': relative + '/app.json', 'local_viewer': relative + '/index.html',
                           'description': app.get('description', ''), 'coverage': 'complete-source-snapshot'})
    for path in sorted(root.glob('imports/**/*.jsonl')):
        from references import normalize, taxonomy
        rules = taxonomy(root)
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            r = normalize(json.loads(line), root, rules)
            if not r.get('screens') or r['id'] in seen:
                continue
            seen.add(r['id'])
            result.append({'id': 'ux:' + r['id'], 'product': r['product'], 'platform': r['platform'],
                           'source': r['source'],
                           'source_url': r['source_url'], 'site_url': r.get('site_url', ''),
                           'kind': r['kind'], 'record_ids': [r['id']], 'flow_count': int(r['kind'] == 'flow'),
                           'screen_count': len(r.get('screens', [])), 'screens': r.get('screens', [])[:3],
                           'captured_label': r.get('captured_label', ''), 'collected_at': r['collected_at'],
                           'local_metadata': r.get('local_metadata', path.relative_to(root).as_posix()),
                           'local_viewer': r.get('local_viewer', ''), 'description': r.get('summary', ''),
                           'coverage': r.get('coverage', 'recorded-page'),
                           'verified_entity_id': r.get('site_entity_id'), 'identity_relation': r.get('identity_relation', 'same-product'),
                           'reference_type': r.get('reference_type', ''),
                           'site_identity_status': r.get('site_identity_status'), 'design_id': r.get('design_id'),
                           'design_id_warning': r.get('design_id_warning'),
                           'identity_evidence': r.get('identity_evidence', [])})
    return result


def match_case(case, resolver, known_ids):
    if case.get('reference_type')=='design-gallery' and (case.get('site_identity_status')!='verified-domain' or not case.get('identity_evidence')):
        return {'entity_id':None,'basis':'independent-gallery-design','relation':'independent-design','confidence':'unlinked','evidence':[]}
    match=resolver.resolve(case['product'],case['site_url'])
    if case.get('reference_type')=='design-gallery' and match.get('entity_id'):
        match.update(relation='design-reference-for-site',evidence=case['identity_evidence'])
    if case.get('verified_entity_id'):
        if case['verified_entity_id'] not in known_ids or not case.get('identity_evidence'):
            raise ValueError('Collected identity requires a known entity and evidence: '+case['id'])
        match={'entity_id':case['verified_entity_id'],'basis':'reviewed-source-identity',
               'confidence':'reviewed','relation':case.get('identity_relation','same-product'),'evidence':case['identity_evidence']}
    return match


def analyze(db, identity, documents, ux):
    counts = dict(db.execute('SELECT category,count(*) FROM rules WHERE brand_id=? GROUP BY category', (identity,)))
    samples = []
    # Prefer short declared values and deterministic combinations; never summarize visual appearance here.
    for category in ('color', 'typography', 'spacing', 'layout', 'components', 'shape', 'motion'):
        rows = db.execute("SELECT data FROM rules WHERE brand_id=? AND category=? AND kind IN ('token','derived') ORDER BY CASE kind WHEN 'derived' THEN 0 ELSE 1 END,property", (identity, category))
        selected = 0
        for (raw,) in rows:
            rule = json.loads(raw)
            if len(json.dumps(rule['value'], ensure_ascii=False)) > 550:
                continue
            samples.append({k: rule[k] for k in ('id', 'document_id', 'category', 'kind', 'property', 'value', 'context')} |
                           {'evidence': rule.get('evidence'), 'inputs': rule.get('inputs', []), 'note': rule.get('derivation_note', '')})
            selected += 1
            if selected == 2:
                break
    return {'method': 'source-document-extraction-and-deterministic-relations', 'rule_counts': counts,
            'samples': samples, 'missing_dimensions': [k for k in ('color', 'typography', 'spacing', 'layout') if not counts.get(k)],
            'document_capture_labels': sorted({d.get('captured_label', '') or 'unknown' for d in documents}),
            'ux_capture_labels': sorted({u.get('captured_label', '') or 'unknown' for u in ux}),
            'limits': ['MD values describe provider-declared surfaces and dates; linked UX may use different surfaces or versions.',
                       'Matching a brand does not verify tokens against screenshots. Inspect selected images before visual claims.',
                       'Different sources may reuse underlying material; source count is not independent corroboration.']}


def build(root):
    with registry_lock(root):
        return _build(root)


def _build(root):
    input_signature = signature(root)
    catalog = read(root / 'knowledge/design-rules/catalog.json')
    aliases = read(root / 'catalog/site-aliases.json')
    for alias in aliases.get('aliases', [])+aliases.get('domain_aliases', []):
        for evidence in alias['evidence']:
            local_file(root, evidence)
    resolver = Resolver(catalog, aliases)
    entities = {b['id']: {**b, 'aliases': [b['name']], 'documents': [], 'ux': [], 'match_status': 'md-only'} for b in catalog['brands']}
    for identity, name in aliases.get('canonical_names', {}).items():
        if identity in entities:
            entities[identity]['name']=name
            entities[identity]['aliases'].append(name)
    for name, identity in aliases.get('search_aliases', {}).items():
        if identity in entities:
            entities[identity]['aliases'].append(name)
    for alias in aliases.get('domain_aliases', []):
        entities[alias['brand_id']]['aliases'].append(alias['host'])
    for d in catalog['documents']:
        entities[d['brand_id']]['documents'].append(d)
        entities[d['brand_id']]['aliases'].extend([d['brand'], d['source_slug']])
        entities[d['brand_id']]['aliases'].extend(d.get('aliases', []))
    cases = ux_cases(root)
    product_sites = defaultdict(dict)
    for case in cases:
        if case['site_url'] and (case.get('reference_type')!='design-gallery' or
                case.get('site_identity_status')=='verified-domain' and case.get('identity_evidence')):
            product_sites[key(case['product'])][host(case['site_url'])] = case['site_url']
    mappings, ambiguities = {}, []
    for case in cases:
        match = match_case(case,resolver,entities)
        bid = match['entity_id']
        if not bid:
            independent = match['basis']=='independent-gallery-design'
            possible_sites = {} if independent else product_sites[key(case['product'])]
            grouped_site = next(iter(possible_sites.values())) if len(possible_sites) == 1 else ''
            domain = host(case['site_url'] or grouped_site)
            name_key = key(case['product']) or unicodedata.normalize('NFKC', case['product']).casefold()
            if independent:
                design_id = case.get('design_id')
                # Keep source identity distinct from its filesystem representation. Using the full
                # reference ID when no design ID exists avoids merging unrelated trailing revisions.
                bid = ('design:gallery:source:' + case['source'] + ':item:' + str(design_id) if design_id is not None else
                       'design:gallery:reference:' + case['record_ids'][0])
            else:
                bid = ('site:' + domain.encode('idna').decode('ascii') if domain else
                       'site:name:' + hashlib.sha256(name_key.encode('utf-8')).hexdigest())
            entities.setdefault(bid, {'id': bid, 'name': case['product'], 'homepage': '' if independent else case['site_url'] or grouped_site,
                                     'entity_type':'independent-design' if independent else 'site',
                                     'aliases': [case['product']], 'documents': [], 'ux': [], 'match_status': 'ux-only'})
            if match.get('candidates'):
                ambiguities.append({'case_id': case['id'], **match})
        entity = entities[bid]
        if not entity['homepage'] and case['site_url'] and entity.get('entity_type')!='independent-design':
            entity['homepage'] = case['site_url']
            entity['homepage_basis'] = case['source_url']
        case['match'] = {**match, 'entity_id': bid}
        entity['ux'].append(case)
        entity['aliases'].append(case['product'])
        for rid in case['record_ids']:
            mappings[rid] = {'entity_id': bid, 'basis': match['basis'], 'relation': match.get('relation', 'unmatched'), 'confidence': match.get('confidence', 'unmatched')}
    doc_by_id = {d['id']: d for d in catalog['documents']}
    with closing(sqlite3.connect((root / 'knowledge/design-rules/rules.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        variations = Counter(v['brand_id'] for v in catalog['variations'])
        for e in entities.values():
            e.setdefault('entity_type','site')
            e['aliases'] = sorted(set(e['aliases']))
            e['match_status'] = 'matched' if e['documents'] and e['ux'] else 'md-only' if e['documents'] else 'ux-only'
            # Arbitrary design IDs may contain quotes or slashes. A full digest is a stable,
            # bounded, case-insensitive-filesystem-safe basename without lossy slug collisions.
            stem = ('design--gallery--' + hashlib.sha256(e['id'].encode('utf-8')).hexdigest()
                    if e['entity_type'] == 'independent-design' else e['id'].replace(':', '--'))
            e['local_viewer'] = 'catalog/sites/' + stem + '.html'
            e['analysis_path'] = 'catalog/sites/' + stem + '.md'
            e['rule_viewer'] = e['local_viewer'] + '#rules' if e['documents'] else None
            e['ux_record_count'] = sum(len(u['record_ids']) for u in e['ux'])
            e['ux_screen_count'] = sum(u['screen_count'] for u in e['ux'])
            e['analysis'] = analyze(db, e['id'], e['documents'], e['ux']) if e['documents'] else None
            e['value_variation_count'] = variations[e['id']]
            e['related_documents'] = [a for a in aliases.get('related_products', []) if key(a['product']) in {key(n) for n in e['aliases']}]
            for d in e['documents']:
                mappings[d['id']] = {'entity_id': e['id'], 'basis': d['brand_match_basis'], 'relation': 'describes-brand', 'confidence': 'source-declared'}
    stats = {'entities': len(entities), 'documents': len(doc_by_id), 'ux_products': len({c['product'] for c in cases}),
             'ux_cases': len(cases), 'ux_records': sum(len(c['record_ids']) for c in cases),
             'matched_ux_products': len({c['product'] for e in entities.values() if e['documents'] for c in e['ux']}),
             'matched_ux_records': sum(e['ux_record_count'] for e in entities.values() if e['documents']),
             'sites':sum(e.get('entity_type')!='independent-design' for e in entities.values()),
             'independent_designs':sum(e.get('entity_type')=='independent-design' for e in entities.values()),
             'statuses': dict(Counter(e['match_status'] for e in entities.values() if e.get('entity_type')!='independent-design')), 'identity_conflicts': len(ambiguities)}
    result = {'schema_version': 1, 'built_at': datetime.now(timezone.utc).isoformat(), 'stats': stats,
              'entities': list(entities.values()), 'record_matches': mappings, 'identity_conflicts': ambiguities,
              'input_signature': input_signature}
    if signature(root) != input_signature:
        raise InputsChanged('Site registry inputs changed during build; retry with the current snapshot')
    save(root / 'catalog/site-matches.json', result)
    return stats


def signature(root):
    paths = {root/'catalog/site-aliases.json', root/'knowledge/design-rules/catalog.json',
             root/'knowledge/design-rules/rules.sqlite'}
    paths.update(root.glob('archives/*/selection.json'))
    paths.update(root.glob('imports/**/*.jsonl'))
    for selection in root.glob('archives/*/selection.json'):
        for app in read(selection)['apps']:
            paths.update(selection.parent/app['archiveDir']/name for name in ('app.json','all-flows.json'))
    return hashlib.sha256(json.dumps(file_signature(root, paths,
        ('library.py', 'site_registry.py', 'build_site_view.py'))).encode()).hexdigest()


def ensure(root):
    if not (root/'catalog/site-aliases.json').exists() or not (root/'knowledge/design-rules/catalog.json').exists():
        return None
    target=root/'catalog/site-matches.json'
    with registry_lock(root):
        # A collector and a desktop read can observe the same stale snapshot.
        # Recheck after taking the lock so only one publishes it and its views.
        data=read(target) if target.exists() else None
        if data is None or data.get('input_signature') != signature(root):
            for attempt in range(3):
                try:
                    _build(root)
                    break
                except InputsChanged:
                    if attempt == 2: raise
            from build_site_view import build as build_view
            build_view(root)
            data=read(target)
        return data


def attach(record, data):
    if not data:
        return record
    match=data['record_matches'].get(record['id'])
    if not match:
        return record
    if '_entity_by_id' not in data:
        data['_entity_by_id']={e['id']:e for e in data['entities']}
    entities=data['_entity_by_id']
    e=entities[match['entity_id']]
    record['site_entity']={**match,'name':e['name'],'homepage':e['homepage'],'local_viewer':e['local_viewer'],
                           'entity_type':e.get('entity_type','site'),
                           'analysis_path':e['analysis_path'],'rule_viewer':e['rule_viewer'],
                           'design_document_ids':[d['id'] for d in e['documents']], 'ux_reference_count':e['ux_record_count'],
                           'scope_note':'Shared identity is a navigation link, not proof that MD rules describe this capture.'}
    return record


def find(root, query):
    data = read(root / 'catalog/site-matches.json')
    query_host = host(query if '://' in query else 'https://' + query) if '.' in query and ' ' not in query else host(query)
    domain_ids={a['brand_id'] for a in read(root/'catalog/site-aliases.json').get('domain_aliases',[]) if a['host']==query_host}
    exact = [e for e in data['entities'] if query == e['id'] or key(query) in {key(x) for x in e['aliases']} or e['id'] in domain_ids or query_host and query_host == host(e['homepage'])]
    if not query.startswith('design:') and any(e.get('entity_type')!='independent-design' for e in exact):
        exact=[e for e in exact if e.get('entity_type')!='independent-design']
    if len(exact) != 1:
        matches = exact or [e for e in data['entities'] if key(query) in key(' '.join(e['aliases']) + ' ' + e['homepage'])]
        return {'matches': [{k: e[k] for k in ('id', 'name', 'homepage', 'match_status', 'local_viewer')} for e in matches[:20]], 'total': len(matches)}
    e = dict(exact[0])
    e['ux'] = [{**{k: v for k, v in u.items() if k not in {'record_ids', 'screens'}}, 'reference_ids': u['record_ids'][:3]} for u in e['ux']]
    for k in ('local_viewer', 'analysis_path', 'rule_viewer'):
        if e.get(k):
            e[k] = str(root / e[k])
    return e


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='User-owned data library, outside this skill')
    parser.add_argument('command', choices=['build', 'stats', 'show'])
    parser.add_argument('query', nargs='?', default='')
    args = parser.parse_args()
    try:
        root = library_root(args.root)
        require_library(root)
        if args.command == 'build':
            result = build(root)
            from build_site_view import build as build_view
            build_view(root)
        else:
            data = ensure(root)
            if data is None:
                raise ValueError('Build the graph first: design_graph.py --root <data-directory> build')
            result = find(root, args.query) if args.command == 'show' else data['stats']
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
