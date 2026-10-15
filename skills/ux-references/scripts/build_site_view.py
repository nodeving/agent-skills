#!/usr/bin/env python3
"""Offline, bidirectional site/UX/DESIGN.md reconciliation browser."""
import argparse
from contextlib import closing
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import quote, urlsplit
import site_registry as registry

from library import library_root, local_file, require_library


def text(value):
    return escape(str(value or ''), quote=True)


def href(path, directory, root):
    return quote(os.path.relpath(root / path, directory), safe='/#')


def external(url, label):
    return f'<a href="{text(url)}" target="_blank" rel="noopener">{text(label)}</a>' if urlsplit(url or '').scheme in {'http', 'https'} else ''


LABELS = {'matched': '문서 + UX 연결', 'md-only': '문서만 있음', 'ux-only': 'UX만 있음'}
DIMENSIONS = {'color': '색 조합', 'typography': '폰트·크기 비율', 'spacing': '간격', 'layout': '배치', 'components': '컴포넌트', 'shape': '모서리', 'motion': '모션'}
CSS = '''*{box-sizing:border-box}body{margin:0;background:#0e1420;color:#e9eef5;font:15px/1.65 system-ui,-apple-system,sans-serif}a{color:#9fdbc8;text-decoration:none}a:hover{text-decoration:underline}header,main,footer{max-width:1360px;margin:auto;padding:30px}h1{font-size:clamp(28px,4vw,48px);line-height:1.25;margin:18px 0}h2{margin-top:32px}h3{margin:0 0 8px}p{max-width:100ch}.muted,small{color:#9aacbf}.stats,.links,.chips{display:flex;gap:10px;flex-wrap:wrap}.stat,.chip{border:1px solid #354353;border-radius:8px;padding:6px 12px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}.card,.panel{padding:20px;border:1px solid #334253;background:#172130;border-radius:14px;margin:12px 0}.toolbar{position:sticky;top:0;z-index:5;background:#101824eb;backdrop-filter:blur(12px);display:flex;gap:12px;flex-wrap:wrap;padding:15px 0}input,select{font:inherit;color:inherit;background:#1c293a;padding:10px;border:1px solid #465a70;border-radius:7px}input{min-width:260px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:16px;border-bottom:1px solid #2c3a4d;vertical-align:top}th{white-space:nowrap}.thumb{display:block;width:100%;height:210px;object-fit:contain;object-position:top;background:#253247;border-radius:8px;margin-bottom:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.65 ui-monospace,monospace;background:#101824;padding:15px;border-radius:9px}code{overflow-wrap:anywhere}.notice{border-left:3px solid #7cc9b7;padding-left:14px}details{margin:14px 0}summary{cursor:pointer}[hidden]{display:none!important}@media(max-width:600px){header,main,footer{padding:20px}input,select{width:100%;min-width:0}.toolbar{position:static}td,th{padding:10px}}'''


def page(title, body, script='', stylesheet='style.css'):
    return f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{text(title)}</title><link rel="stylesheet" href="{text(stylesheet)}"></head><body>{body}{script}</body></html>'


def analysis_markdown(e):
    lines = [f"# {e['name']} · UX / DESIGN.md 연결 분석", '', f"- Entity: `{e['id']}`", f"- 상태: {LABELS[e['match_status']]}",
             f"- 실제 사이트(출처 기록): {e['homepage'] or '미확인'}", f"- 출처 문서: {len(e['documents'])}개 · UX 자료: {len(e['ux'])}묶음 / {e['ux_record_count']}개 레코드", '',
             '이 파일은 로컬 원문에 대한 생성된 분석입니다. 원본 DESIGN.md와 구분하며, 스크린샷에서 디자인 토큰을 실측한 결과가 아닙니다.', '', '## 연결 근거', '']
    for u in e['ux']:
        m = u['match']
        lines.extend([f"- {u['product']} / {u['platform']} / {u['captured_label'] or '원본 시점 미확인'}: {m['basis']} · {m.get('relation', 'unmatched')}",
                      f"  - {u.get('source', 'app-archive')}: {u['source_url']}", f"  - 로컬 화면: {u.get('local_viewer') or u['local_metadata']}", f"  - 수집 범위: {u['coverage']}"])
        if m.get('note'):
            lines.append('  - ' + m['note'])
    lines.extend(['', '## 원본 문서', ''])
    for d in e['documents']:
        lines.extend([f"- {d['source']} · {d['captured_label'] or '시점 미확인'} · 제공자 등급 {d['quality_declared']}", f"  - {d['source_url']}", f"  - {d['path']}", f"  - SHA256: {d['sha256']}"])
    if e['analysis']:
        a=e['analysis']
        lines.extend(['', '## 규칙 분석', '', f"출처별 값 차이가 있는 속성: {e['value_variation_count']}개. 오류로 단정하지 말고 화면·테마·시점·단위를 비교합니다.", ''])
        for s in a['samples']:
            lines.extend([f"### {DIMENSIONS.get(s['category'], s['category'])} · {s['property']}", '', '```json', json.dumps(s['value'],ensure_ascii=False,indent=2), '```',
                          f"- 종류: {s['kind']} · 규칙 ID: `{s['id']}`", f"- 문서 ID: `{s['document_id']}`", '- 맥락: '+json.dumps(s['context'],ensure_ascii=False),
                          '- 근거: '+json.dumps(s['evidence'] or {'input_rule_ids':s['inputs']},ensure_ascii=False), ''])
        lines.extend(['## 적용 판단', '', '색·폰트·간격을 선택할 때 동일 문서와 화면 맥락을 우선합니다. 앱 화면과 마케팅 사이트의 토큰을 자동으로 동일하게 적용하지 않습니다.',
                      '서로 다른 폰트 크기 단위나 테마의 색을 평균 내지 않습니다. 프로젝트에서 채택할 값·이유·예외·실제 렌더링 검증을 별도로 기록합니다.',
                      '연결된 화면은 디자인 규칙의 사실성을 증명하지 않습니다. 시각적 결론을 낼 때 선택한 원본 이미지를 직접 확인합니다.', ''])
    else:
        lines.extend(['', '현재 로컬 MD 출처에서 동일 사이트 문서를 찾지 못했습니다. 관련 모기업 문서는 동일 사이트 문서로 취급하지 않습니다.', ''])
    return '\n'.join(lines)


def document_view(root, directory, document):
    relative=f"catalog/sites/documents/{document['source']}--{document['source_slug']}.html"
    target=root/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    source=local_file(root,document['path'])
    lines='\n'.join(f'<span id="L{i}"><a href="#L{i}">{i:4}</a> {text(line)}</span>'
                    for i,line in enumerate(source.read_text(encoding='utf-8').splitlines(),1))
    body=f'<header><a href="../index.html">← 사이트별 자료실</a><h1>{text(document["brand"])}</h1>{external(document["source_url"],"출처")}<p>SHA256: {text(document["sha256"])}</p></header><main><pre>{lines}</pre></main>'
    target.write_text(page(document['brand']+' · source',body,stylesheet='../style.css'),encoding='utf-8')
    return relative


def rule_view(root,directory,entity):
    cards=[]
    with closing(sqlite3.connect((root/'knowledge/design-rules/rules.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        for (raw,) in db.execute('SELECT data FROM rules WHERE brand_id=? ORDER BY category,property',(entity['id'],)):
            rule=json.loads(raw)
            evidence=rule.get('evidence')
            link=''
            if evidence:
                document=next(d for d in entity['documents'] if d['id']==rule['document_id'])
                target=f"catalog/sites/documents/{document['source']}--{document['source_slug']}.html#L{evidence['start_line']}"
                link=f'<a href="{href(target,directory,root)}">원문 {evidence["start_line"]}행</a>'
            cards.append(f'<details><summary>{text(rule["category"])} · {text(rule["property"])} · {text(rule["kind"])}</summary><pre>{text(json.dumps(rule,ensure_ascii=False,indent=2))}</pre>{link}</details>')
    return '<section id="rules"><h2>모든 규칙·조합·비율</h2>'+''.join(cards)+'</section>'


def build(root, link_viewers=False):
    data=registry.read(root/'catalog/site-matches.json'); directory=root/'catalog/sites';directory.mkdir(parents=True,exist_ok=True)
    (directory/'style.css').write_text(CSS,encoding='utf-8')
    rows=[]
    for e in sorted(data['entities'],key=lambda x:x['name'].casefold()):
        filename=Path(e['local_viewer']).name
        (root/e['analysis_path']).write_text(analysis_markdown(e),encoding='utf-8')
        doc_cards=[]
        for d in e['documents']:
            viewer=document_view(root, directory, d)
            doc_cards.append(f'<article class="card"><h3>{text(d["source"])}</h3><p>{text(d["brand"])}</p><p>원본 시점 {text(d["captured_label"] or "미확인")} · {text(d["quality_declared"])}</p><div class="links"><a href="{href(viewer,directory,root)}">원문·줄 번호</a><a href="{href(d["path"],directory,root)}">DESIGN.md</a>{external(d["source_url"],"출처")}</div></article>')
        ux_cards=[]
        for u in e['ux']:
            visual=f'<a href="{href(u["screens"][0]["path"],directory,root)}"><img class="thumb" loading="lazy" src="{href(u["screens"][0]["path"],directory,root)}" style="{'object-fit:cover' if u['kind']=='page' else ''}" alt="{text(u["product"])} 저장된 첫 화면"></a>' if u['screens'] else ''
            viewer=f'<a href="{href(u["local_viewer"],directory,root)}">전체 저장 자료</a>' if u.get('local_viewer') else ''
            m=u['match']
            ux_cards.append(f'<article class="card">{visual}<h3>{text(u["product"])} · {text(u["platform"])}</h3><p>{u["flow_count"]} Flow · 화면 {u["screen_count"]}장</p><small>원본 {text(u["captured_label"] or "시점 미확인")} · 수집 {text(u["collected_at"][:10])}</small><p>{text(u["description"][:180])}</p><p class="muted">{text(u["coverage"])}</p><div class="links">{viewer}{external(u["source_url"],u.get("source","app-archive")+" 원본")}</div><details><summary>매칭 근거</summary><p>{text(m["basis"])} · {text(m.get("relation",""))}</p><p>{text(m.get("note",""))}</p><pre>{text(json.dumps(m.get("evidence",[]),ensure_ascii=False,indent=2))}</pre></details></article>')
        analysis=''
        if e['analysis']:
            samples=[]
            for s in e['analysis']['samples']:
                evidence=s.get('evidence')
                evidence_html=''
                if evidence:
                    document=next(d for d in e['documents'] if d['id']==s['document_id'])
                    target=f"catalog/sites/documents/{document['source']}--{document['source_slug']}.html#L{evidence['start_line']}"
                    evidence_html=f'<a href="{href(target,directory,root)}">원문 {evidence["start_line"]}행</a>'
                samples.append(f'<article class="card"><small>{text(DIMENSIONS.get(s["category"],s["category"]))} · {text(s["kind"])}</small><h3><code>{text(s["property"])}</code></h3><pre>{text(json.dumps(s["value"],ensure_ascii=False,indent=2))}</pre><p class="muted">{text(s.get("note",""))}</p>{evidence_html}</article>')
            analysis=f'<h2>문서에 근거한 규칙 분석</h2><p>{e["value_variation_count"]}개 속성에 출처별 값 차이가 있습니다. 전체 규칙 페이지에서 맥락과 계산 입력을 확인하세요.</p><div class="links"><a href="{href(e["rule_viewer"],directory,root)}">모든 규칙·조합·비율 탐색</a><a href="{text(Path(e["analysis_path"]).name)}">분석 MD</a></div><div class="cards">{"".join(samples)}</div>'
        analysis += rule_view(root, directory, e) if e['documents'] else ''
        related=''
        if e['related_documents']:
            related='<p>관련 브랜드 문서: '+', '.join(f'<a href="{text(a["brand_id"].replace(":","--"))}.html">{text(a["brand_id"])}</a>' for a in e['related_documents'])+' · 동일 사이트로 병합하지 않았습니다.</p>'
        body=f'<header><a href="index.html">← 사이트별 통합 자료실</a><h1>{text(e["name"])}</h1><p class="chip">{LABELS[e["match_status"]]}</p><div class="links">{external(e["homepage"],"실제 사이트")}<a href="{text(Path(e["analysis_path"]).name)}">AI용 연결 분석 MD</a></div><p class="muted">Entity: {text(e["id"])}</p><div class="stats"><span class="stat">문서 {len(e["documents"])}개</span><span class="stat">UX 레코드 {e["ux_record_count"]:,}개</span><span class="stat">화면 참조 {e["ux_screen_count"]:,}장</span></div></header><main><p class="notice">문서의 규칙과 화면은 출처·화면 범위·캡처 시점을 보존해 연결합니다. 같은 브랜드라도 모든 화면이 같은 디자인 규칙을 쓰는 것은 아닙니다.</p>{related}<h2>DESIGN.md 원본</h2><div class="cards">{"".join(doc_cards) or "현재 공개 원문에서 매칭된 문서가 없습니다."}</div><h2>저장된 UX 자료</h2><div class="cards">{"".join(ux_cards) or "아직 저장된 UX 화면이 없습니다. 로컬 JSONL 자료를 가져오면 여기에 표시됩니다."}</div>{analysis}</main><footer><a href="index.html">사이트별 자료실</a> · <a href="../site-matches.json">연결 데이터 JSON</a></footer>'
        (directory/filename).write_text(page(e['name']+' · UX와 디자인 규칙',body),encoding='utf-8')
        if e.get('entity_type') == 'independent-design':
            continue
        searchable=' '.join(e['aliases'])+' '+e['homepage']+' '+e['id']
        rows.append(f'<tr data-search="{text(searchable.casefold())}" data-status="{e["match_status"]}"><td><a href="{text(filename)}"><strong>{text(e["name"])}</strong></a><br><small>{text(registry.host(e["homepage"]))}</small></td><td>{LABELS[e["match_status"]]}</td><td>{len(e["documents"])}개</td><td>{e["ux_record_count"]:,}개</td><td><a href="{text(filename)}">자료·분석 보기</a></td></tr>')
    s=data['stats'];status=s['statuses']
    body=f'<header><a href="../site-matches.json">연결 데이터 JSON</a><h1>사이트별 UX × DESIGN.md</h1><p>동일 사이트의 화면과 디자인 규칙을 한곳에서 찾습니다. 도메인, 출처 이름, 검토된 별칭으로 연결하고 매칭 근거를 남겼습니다.</p><div class="stats"><span class="stat">연결 {status.get("matched",0)}개</span><span class="stat">문서만 {status.get("md-only",0)}개</span><span class="stat">UX만 {status.get("ux-only",0)}개</span><span class="stat">연결 UX 레코드 {s["matched_ux_records"]:,}개</span></div><p class="muted">색 조합 · 폰트 조합 · 크기 비율 · 배치 · 간격 · 컴포넌트</p><div class="links"><a href="../../knowledge/design-rules/catalog.json">규칙 카탈로그 JSON</a><a href="../site-matches.json">AI용 연결 JSON</a></div></header><main><div class="toolbar"><input id="q" type="search" placeholder="사이트 이름, 도메인, 별칭" aria-label="사이트 검색"><select id="status" aria-label="연결 상태"><option value="">전체</option><option value="matched">문서 + UX 연결</option><option value="md-only">문서만 있음</option><option value="ux-only">UX만 있음</option></select></div><p id="count" aria-live="polite"></p><div class="table-wrap"><table><thead><tr><th>사이트·브랜드</th><th>연결 상태</th><th>MD</th><th>UX 레코드</th><th>열기</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></main><footer>문서 출처의 품질 등급은 제공자 선언입니다. 화면 범위와 시점이 다른 자료를 자동으로 하나의 디자인 규칙으로 합치지 않습니다.</footer>'
    script='<script>const q=document.getElementById("q"),s=document.getElementById("status"),rows=[...document.querySelectorAll("tbody tr")];function filter(){let n=0;const terms=q.value.toLocaleLowerCase().trim().split(/\\s+/).filter(Boolean);for(const r of rows){r.hidden=!terms.every(t=>r.dataset.search.includes(t))||!!s.value&&r.dataset.status!==s.value;if(!r.hidden)n++;}document.getElementById("count").textContent=n+"개 사이트 표시";}q.addEventListener("input",filter);s.addEventListener("input",filter);filter();</script>'
    collection_galleries=sorted(root.glob('archives/*/index.html'),reverse=True)
    if collection_galleries:
        links=''.join(f'<a href="{href(p.relative_to(root),directory,root)}">{text(p.parent.name)} collection</a>' for p in collection_galleries)
        body=body.replace('</header>',f'<details><summary>저장된 수집 갤러리</summary><div class="links">{links}</div></details></header>',1)
    (directory/'index.html').write_text(page('사이트별 UX × DESIGN.md',body,script),encoding='utf-8')
    # Updating an existing app viewer is opt-in; search refreshes never take this path.
    for e in data['entities'] if link_viewers else []:
        for case in e['ux']:
            if case['kind']!='app-archive' or not case.get('local_viewer'):
                continue
            relative=case['local_viewer'].split('#',1)[0]
            path=local_file(root,relative)
            if path.suffix.lower() not in {'.html','.htm'}:
                raise ValueError('Viewer must be an HTML file: '+relative)
            original=path.read_text(encoding='utf-8')
            link=f'<a class="button" id="site-entity-link" href="{href(e["local_viewer"],path.parent,root)}">사이트 매칭·DESIGN.md·분석</a>'
            if 'id="site-entity-link"' in original:
                updated=re.sub(r'<a[^>]+id="site-entity-link"[^>]*>.*?</a>',lambda _:link,original,count=1)
            else:
                updated=original.replace('<header>','<header>'+link,1)
            if updated!=original:path.write_text(updated,encoding='utf-8')
    return {'entities':len(rows),'index':str(directory/'index.html'),'linked_existing_viewers':link_viewers}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True,help='User-owned data library, outside this skill')
    parser.add_argument('--link-viewers',action='store_true',help='Modify existing app-archive HTML to add or update backlinks; default only writes generated catalog/sites files')
    args=parser.parse_args()
    try:
        root=library_root(args.root)
        require_library(root)
        if registry.ensure(root) is None:
            raise ValueError('Build the graph first: design_graph.py --root <data-directory> build')
        print(json.dumps(build(root,link_viewers=args.link_viewers),ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({'error':str(error)}),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
