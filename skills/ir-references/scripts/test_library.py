"""Behavior checks for refresh, relationships, and analysis provenance."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import os
import subprocess
import sys
import copy
import stat
import io

import ir_library as lib


def fixture_pptx(path):
    # Deliberately nonsequential slide/notes filenames test relationship-based pairing.
    parts = {
        'ppt/presentation.xml': f'<p:presentation xmlns:p="{lib.NS["p"]}" xmlns:r="{lib.NS["r"]}"><p:sldIdLst><p:sldId id="256" r:id="rFirst"/><p:sldId id="257" r:id="rSecond"/></p:sldIdLst></p:presentation>',
        'ppt/_rels/presentation.xml.rels': '<Relationships><Relationship Id="rFirst" Target="slides/slide7.xml"/><Relationship Id="rSecond" Target="slides/slide2.xml"/></Relationships>',
        'ppt/slides/slide7.xml': f'<p:sld xmlns:p="{lib.NS["p"]}" xmlns:a="{lib.NS["a"]}"><a:p><a:r><a:t>고객의 질문</a:t></a:r></a:p></p:sld>',
        'ppt/slides/slide2.xml': f'<p:sld xmlns:p="{lib.NS["p"]}" xmlns:a="{lib.NS["a"]}"><a:p><a:r><a:t>제품의 답</a:t></a:r></a:p></p:sld>',
        'ppt/slides/_rels/slide7.xml.rels': '<Relationships><Relationship Id="n" Type="notesSlide" Target="../notesSlides/notesSlide9.xml"/></Relationships>',
        'ppt/notesSlides/notesSlide9.xml': f'<p:notes xmlns:p="{lib.NS["p"]}" xmlns:a="{lib.NS["a"]}"><p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><a:p><a:r><a:t>반복 확인이 고객의 부담입니다.</a:t></a:r></a:p></p:sp><p:sp><p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr><a:p><a:r><a:t>999</a:t></a:r></a:p></p:sp></p:notes>'
    }
    with zipfile.ZipFile(path, 'w') as z:
        for name, value in parts.items():
            z.writestr(name, value)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '자료'
        self.state = Path(self.temp.name) / '상태'
        self.root.mkdir()

    def sync(self):
        return lib.sync(self.root, self.state)[0]

    def test_cli_requires_source_and_keeps_default_state_outside_skill(self):
        script = str(Path(lib.__file__))
        missing = subprocess.run([sys.executable, script, 'sync'], capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertIn('--root', missing.stderr)
        (self.root / 'example.md').write_text('Synthetic reference only', encoding='utf-8')
        state_home = Path(self.temp.name) / 'xdg-state'
        result = subprocess.run([sys.executable, script, '--root', str(self.root), 'sync'],
                                env={**os.environ, 'XDG_STATE_HOME': str(state_home)},
                                check=True, capture_output=True, text=True)
        state = Path(json.loads(result.stdout)['state'])
        self.assertIn(state_home.resolve(), state.parents)
        self.assertNotIn(lib.SKILL, state.parents)
        self.assertEqual(json.loads((state / 'catalog.json').read_text(encoding='utf-8'))['root'], str(self.root.resolve()))
        second_root = Path(self.temp.name) / 'other-library'
        second_root.mkdir()
        second = subprocess.run([sys.executable, script, '--root', str(second_root), 'sync'],
                                env={**os.environ, 'XDG_STATE_HOME': str(state_home)},
                                check=True, capture_output=True, text=True)
        self.assertNotEqual(json.loads(second.stdout)['state'], str(state))

    def test_cli_rejects_state_inside_skill(self):
        result = subprocess.run([sys.executable, str(Path(lib.__file__)), '--root', str(self.root),
                                 '--state', str(lib.SKILL / 'private-state'), 'sync'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('outside the skill folder', result.stderr)

    def test_cli_json_supports_ascii_stdout_and_utf8_state(self):
        (self.root / '회사.md').write_text('한국어 합성 자료', encoding='utf-8')
        env = {**os.environ, 'PYTHONIOENCODING': 'ascii', 'PYTHONUTF8': '0',
               'LC_ALL': 'C', 'PYTHONCOERCECLOCALE': '0'}
        command = [sys.executable, str(Path(lib.__file__)), '--root', str(self.root), '--state', str(self.state)]
        result = subprocess.run([*command, 'source', '회사.md'], env=env, check=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout.decode('ascii'))['units'][0]['text'], '한국어 합성 자료')
        card = self.card(json.loads((self.state / 'catalog.json').read_text(encoding='utf-8')))
        input_file = Path(self.temp.name) / 'analysis.json'
        input_file.write_text(json.dumps(card, ensure_ascii=False), encoding='utf-8')
        subprocess.run([*command, 'record', str(input_file)], env=env, check=True, capture_output=True)
        result = subprocess.run([*command, 'show', card['id']], env=env, check=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout.decode('ascii'))['analysis']['title'], '회사')
        invalid = subprocess.run([*command, 'source', '없는 자료'], env=env, capture_output=True)
        self.assertEqual(invalid.returncode, 1)
        self.assertIn('error', json.loads(invalid.stderr.decode('ascii')))
        help_result = subprocess.run([sys.executable, str(Path(lib.__file__)), '--help'],
                                     env=env, check=True, capture_output=True)
        self.assertIn('usage:', help_result.stdout.decode('ascii'))

    def test_cli_utf8_stdout_keeps_original_korean(self):
        (self.root / '회사.md').write_text('한국어 합성 자료', encoding='utf-8')
        result = subprocess.run([sys.executable, str(Path(lib.__file__)), '--root', str(self.root),
                                 '--state', str(self.state), 'source', '회사.md'],
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                                check=True, capture_output=True)
        self.assertIn('한국어 합성 자료', result.stdout.decode('utf-8'))

    def test_utf8_strict_json_stream_escapes_lone_surrogate(self):
        with io.BytesIO() as buffer:
            stream = io.TextIOWrapper(buffer, encoding='utf-8', errors='strict')
            data = {'text': '한국어 합성 자료', 'invalid_unicode': '\ud800'}
            lib.print_json(data, stream)
            stream.flush()
            self.assertEqual(json.loads(buffer.getvalue().decode('utf-8')), data)
            stream.detach()

    @unittest.skipUnless(os.name == 'posix', 'Unix permission bits are required')
    def test_cli_new_state_is_private_and_existing_directory_mode_is_preserved(self):
        (self.root / '회사.md').write_text('합성 자료', encoding='utf-8')
        command = [sys.executable, str(Path(lib.__file__)), '--root', str(self.root)]
        subprocess.run([*command, '--state', str(self.state), 'report'], check=True, capture_output=True)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for path in self.state.rglob('*'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
        existing = Path(self.temp.name) / 'existing-state'
        existing.mkdir(mode=0o755)
        existing.chmod(0o755)
        subprocess.run([*command, '--state', str(existing), 'report'], check=True, capture_output=True)
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((existing / 'analysis-report.md').stat().st_mode), 0o600)

    def card(self, catalog, source='회사.md'):
        return {'id': 'EXAMPLE', 'title': '회사', 'source': source,
                'source_hashes': {source: catalog['sources'][source]['sha256']},
                'summary': '고객의 질문과 해답', 'flow': '질문 → 답', 'strengths': ['같은 사례의 전후 비교'],
                'strategy': '질문을 만든 뒤 증거로 답한다', 'application': 'B2B 도구',
                'script_use': '말은 원인을 설명한다', 'limitations': ['실제 매출은 별도 확인'],
                'evidence': [{'source': source, 'locator': 'lines 1-2', 'observation': '실제 질문과 답'}],
                'tags': ['고객 질문'], 'review_scope': '텍스트 확인'}

    def test_slide_order_and_notes_relationship(self):
        fixture_pptx(self.root / 'deck.pptx')
        units = lib.pptx_units(self.root / 'deck.pptx')
        self.assertEqual([u['slide_text'] for u in units], ['고객의 질문', '제품의 답'])
        self.assertEqual(units[0]['notes'], '반복 확인이 고객의 부담입니다.')
        self.assertEqual(units[1]['notes'], '')

    def test_add_change_delete_and_retirement(self):
        p = self.root / '회사.md'
        p.write_text('고객 질문\n첫 답', encoding='utf-8')
        c = self.sync()
        card = self.card(c)
        lib.record_analysis(card, c, self.state)
        self.assertEqual(lib.pending(c, [card]), [])
        self.assertEqual(lib.card_status(card, c), 'current')
        old_stat = p.stat()
        p.write_text('고객 질문\n새 답', encoding='utf-8')
        os.utime(p, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        c = self.sync()
        self.assertEqual(lib.card_status(card, c), 'needs_review')
        self.assertEqual(lib.pending(c, [card])[0]['status'], 'needs_review')
        nested = self.root / '새 폴더'
        nested.mkdir()
        (nested / '추가.md').write_text('추가 자료', encoding='utf-8')
        c = self.sync()
        self.assertIn('새 폴더/추가.md', c['sources'])
        p.unlink()
        c = self.sync()
        self.assertEqual(lib.card_status(card, c), 'source_missing')
        self.assertFalse(any(item.get('id') == card['id'] for item in lib.pending(c, [card])))
        report = lib.report(c, [card], self.state)
        self.assertEqual(report['cards'], 0)

    def test_read_hash_must_match_and_dependency_change_invalidates(self):
        (self.root / '회사.md').write_text('회사 분석', encoding='utf-8')
        (self.root / '근거.md').write_text('확인 범위', encoding='utf-8')
        c = self.sync()
        card = self.card(c)
        card['source_hashes']['근거.md'] = c['sources']['근거.md']['sha256']
        (self.root / '근거.md').write_text('수정된 확인 범위', encoding='utf-8')
        changed = self.sync()
        with self.assertRaisesRegex(ValueError, 'hashes'):
            lib.record_analysis(card, changed, self.state)

    def test_failed_extract_does_not_serve_previous_text(self):
        fixture_pptx(self.root / 'deck.pptx')
        c = self.sync()
        self.assertEqual(len(c['sources']['deck.pptx']['units']), 2)
        (self.root / 'deck.pptx').write_text('incomplete save', encoding='utf-8')
        c = self.sync()
        self.assertIn('error', c['sources']['deck.pptx'])
        self.assertEqual(c['sources']['deck.pptx']['units'], [])

    def test_missing_source_folder_preserves_catalog(self):
        (self.root / '회사.md').write_text('분석', encoding='utf-8')
        self.sync()
        previous = (self.state / 'catalog.json').read_bytes()
        with self.assertRaises(ValueError):
            lib.sync(self.root / '없는 폴더', self.state)
        self.assertEqual((self.state / 'catalog.json').read_bytes(), previous)

    def test_ppt_analysis_requires_script_review(self):
        fixture_pptx(self.root / 'deck.pptx')
        c = self.sync()
        card = self.card(c, 'deck.pptx')
        with self.assertRaisesRegex(ValueError, 'script_analysis'):
            lib.record_analysis(card, c, self.state)
        card['script_analysis'] = {'matched_slides':[1], 'screen_voice_relation':'질문에 원인 보충',
                                   'effective_connection':'질문 다음 제품', 'improvement':'적용 범위 구분'}
        lib.record_analysis(card, c, self.state)
        pairs = list(lib.script_pairs(card, c['sources']['deck.pptx'], c))
        self.assertTrue(pairs[0]['reviewed_pair'])
        self.assertFalse(pairs[1]['reviewed_pair'])
        self.assertIn('반복 확인', pairs[0]['speaker_notes'])

    def test_script_pairs_extract_generic_reference_urls(self):
        fixture_pptx(self.root / 'deck.pptx')
        catalog = self.sync()
        card = self.card(catalog, 'deck.pptx')
        source = catalog['sources']['deck.pptx']
        urls = ['https://example.invalid/reference#page=2', 'http://example.invalid/notes']
        source['units'][0]['notes'] += '\n' + '\n'.join(urls)
        pair = list(lib.script_pairs(card, source, catalog))[0]
        self.assertEqual(pair['reference_urls'], urls)

    def test_reference_url_candidates_trim_context_and_preserve_balanced_parentheses(self):
        cases = [
            ('(HTTPS://example.invalid/a).', 'HTTPS://example.invalid/a'),
            ('https://example.invalid/a,', 'https://example.invalid/a'),
            ('https://example.invalid/wiki/Foo_(bar)', 'https://example.invalid/wiki/Foo_(bar)'),
            ('(https://example.invalid/wiki/Foo_(bar)).', 'https://example.invalid/wiki/Foo_(bar)'),
        ]
        for notes, expected in cases:
            with self.subTest(notes=notes):
                self.assertEqual(lib.reference_urls(notes), [expected])

    def test_non_pptx_rejects_script_analysis_and_old_cards_can_be_reported(self):
        (self.root / '회사.md').write_text('합성 자료', encoding='utf-8')
        catalog = self.sync()
        for kind in ['md', 'txt', 'pdf', 'xlsx']:
            with self.subTest(kind=kind):
                typed_catalog = copy.deepcopy(catalog)
                typed_catalog['sources']['회사.md']['kind'] = kind
                card = self.card(typed_catalog)
                lib.record_analysis(card, typed_catalog, self.state)
                # Simulate a pre-fix saved card copied from the PPTX template.
                card['script_analysis'] = {'matched_slides': [1, 99], 'screen_voice_relation': '합성 설명',
                                           'effective_connection': '합성 연결', 'improvement': '합성 보완'}
                with self.assertRaisesRegex(ValueError, 'only supported for PPTX'):
                    lib.record_analysis(card, typed_catalog, self.state)
                result = lib.report(typed_catalog, [card], self.state)
                self.assertEqual(result['cards'], 1)
                self.assertNotIn('제공 발표자 노트', Path(result['report']).read_text(encoding='utf-8'))
                with self.assertRaisesRegex(ValueError, 'only supported for PPTX'):
                    list(lib.script_pairs(card, typed_catalog['sources']['회사.md'], typed_catalog))

    def test_korean_normalization_and_source_line_locators(self):
        self.assertGreater(lib.match_score('고객 질문', '고객의 질문을 설명'), 0)
        self.assertEqual(lib.match_score('고객 가격', '고객의 질문을 설명'), 0)
        self.assertGreater(lib.match_score('고객 가격', '고객의 질문을 설명', True), 0)
        p = self.root / '회사.md'
        p.write_text('# 주제\n내용\n## 새 주제\n다른 내용', encoding='utf-8')
        units = lib.extract(p)
        self.assertEqual([u['locator'] for u in units], ['lines 1-2', 'lines 3-4'])

    def test_xlsx_actual_sheet_order_shared_strings_and_hyperlinks(self):
        path = self.root / '표.xlsx'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('xl/workbook.xml', f'<workbook xmlns="{lib.NS["s"]}" xmlns:r="{lib.NS["r"]}"><sheets><sheet name="발화" sheetId="1" r:id="s1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="s1" Target="/xl/worksheets/sheet8.xml"/></Relationships>')
            z.writestr('xl/sharedStrings.xml', f'<sst xmlns="{lib.NS["s"]}"><si><t>고객 질문</t></si></sst>')
            z.writestr('xl/worksheets/sheet8.xml', f'<worksheet xmlns="{lib.NS["s"]}" xmlns:r="{lib.NS["r"]}"><sheetData><row r="4"><c r="A4" t="s"><v>0</v></c><c r="B4" t="inlineStr"><is><t>화면 비교</t></is></c></row></sheetData><hyperlinks><hyperlink ref="B4" r:id="url"/></hyperlinks></worksheet>')
            z.writestr('xl/worksheets/_rels/sheet8.xml.rels', '<Relationships><Relationship Id="url" Target="https://example.com/video?t=20" TargetMode="External"/></Relationships>')
        units = lib.xlsx_units(path)
        self.assertEqual(units[0]['locator'], 'sheet 발화 row 4')
        self.assertIn('A4: 고객 질문', units[0]['text'])
        self.assertIn('https://example.com/video?t=20', units[0]['text'])


if __name__ == '__main__':
    unittest.main()
