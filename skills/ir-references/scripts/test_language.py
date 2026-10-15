import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

import ir_language as lang
import ir_library as lib
import test_library


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.state = base / 'sources', base / 'state'
        self.root.mkdir()
        test_library.fixture_pptx(self.root / 'deck.pptx')
        self.catalog, _ = lib.sync(self.root, self.state)
        self.card = test_library.LibraryTests.card(self, self.catalog, 'deck.pptx')
        self.card['script_analysis'] = {'matched_slides':[1], 'screen_voice_relation':'질문에 원인 보충',
                                       'effective_connection':'질문 다음 제품', 'improvement':'조건 구분'}
        lib.record_analysis(self.card, self.catalog, self.state)
        self.profile = {'id':'EXAMPLE','source':'deck.pptx',
                        'source_sha256':self.catalog['sources']['deck.pptx']['sha256'],
                        'coverage':{'reviewed_note_slides':[1,2],'scope':'두 장의 노트 확인; 둘째 장은 없음'},
                        'selection_note':'고객 부담을 설명하는 문장 선택',
                        'tone':{field:'근거에 연결한 테스트 분석' for field in lang.TONE_FIELDS},
                        'tone_evidence':[{'slide':1,'quote':'반복 확인이 고객의 부담입니다.',
                                          'supports':list(lang.TONE_FIELDS)}],
                        'expressions':[{'id':'EXAMPLE-E01','slide':1,'quote':'반복 확인이 고객의 부담입니다.',
                                        'role':'문제 제기','why_good':'반복 작업을 고객 부담으로 설명',
                                        'pattern':'[반복 작업]이 [고객의 부담]입니다.',
                                        'usage':'고객 문제를 정의할 때', 'caution':'새 고객의 실제 문제인지 확인'}]}

    def command(self, *args):
        result = subprocess.run([sys.executable,str(Path(lib.__file__)), '--root',str(self.root),
                                 '--state',str(self.state),*args],check=True,capture_output=True,text=True)
        return json.loads(result.stdout)

    def save(self):
        data = lang.validate(copy.deepcopy(self.profile), self.catalog, [self.card])
        data['analyzed_at'] = lib.now()
        lib.atomic_json(self.state / 'language/EXAMPLE.json', data)
        return data

    def alter_note(self):
        path = self.root / 'deck.pptx'
        with zipfile.ZipFile(path) as z:
            parts = {name:z.read(name) for name in z.namelist()}
        name = 'ppt/notesSlides/notesSlide9.xml'
        parts[name] = parts[name].replace('반복 확인이 고객의 부담입니다.'.encode(), '수정된 고객 문제입니다.'.encode())
        with zipfile.ZipFile(path, 'w') as z:
            for name, data in parts.items():
                z.writestr(name, data)

    def test_tone_required_before_source_is_fully_analyzed(self):
        pending = lib.pending(self.catalog, [self.card])
        self.assertEqual(pending[0]['status'], 'needs_language_analysis')
        profile = self.save()
        self.assertEqual(lib.pending(self.catalog, [self.card], [profile]), [])

    def test_edited_or_invented_quote_is_rejected(self):
        invalid = copy.deepcopy(self.profile)
        invalid['expressions'][0]['quote'] = '반복 확인은 고객의 부담입니다.'
        with self.assertRaisesRegex(ValueError, 'not verbatim'):
            lang.validate(invalid, self.catalog, [self.card])
        valid = copy.deepcopy(self.profile)
        valid['expressions'][0]['pattern'] = '새 원고에 적용할 별도 문장 틀'
        self.assertEqual(lang.validate(valid,self.catalog,[self.card])['expressions'][0]['quote'],
                         '반복 확인이 고객의 부담입니다.')

    def test_quote_cannot_be_assigned_to_wrong_slide(self):
        invalid = copy.deepcopy(self.profile)
        invalid['expressions'][0]['slide'] = 2
        with self.assertRaisesRegex(ValueError, 'not verbatim'):
            lang.validate(invalid, self.catalog, [self.card])

    def test_quote_must_be_inside_reviewed_coverage(self):
        invalid = copy.deepcopy(self.profile)
        invalid['coverage']['reviewed_note_slides'] = [2]
        with self.assertRaisesRegex(ValueError, 'not verbatim'):
            lang.validate(invalid, self.catalog, [self.card])

    def test_source_change_excludes_stale_language_and_refreshes_report(self):
        self.save()
        self.assertEqual(self.command('phrases','고객')['match_count'],1)
        self.command('report')
        self.alter_note()
        result = self.command('phrases','고객')
        self.assertEqual(result['match_count'],0)
        self.assertGreater(result['pending_count'],0)
        tone = self.command('tone','EXAMPLE')
        self.assertEqual(tone['status'],'needs_review')
        self.assertNotIn('tone_analysis',tone)
        self.assertNotIn('반복 확인이 고객의 부담입니다.',(self.state/'expression-bank.md').read_text(encoding='utf-8'))
        changed,_=lib.sync(self.root,self.state)
        with self.assertRaisesRegex(ValueError,'hash changed'):
            lang.validate(self.profile,changed,[self.card])

    def test_deleted_source_is_removed_from_current_bank(self):
        self.save()
        (self.root/'deck.pptx').unlink()
        self.assertEqual(self.command('phrases')['match_count'],0)
        result=self.command('report')
        self.assertEqual(result['language_profiles'],0)
        self.assertNotIn('반복 확인이 고객의 부담입니다.',(self.state/'expression-bank.md').read_text(encoding='utf-8'))

    def test_new_source_then_tone_record_completes_workflow(self):
        self.save()
        path=self.root/'new.pptx'
        test_library.fixture_pptx(path)
        fresh=self.command('sync')
        self.assertTrue(any(p['source']=='new.pptx' and p['status']=='not_analyzed' for p in fresh['pending']))
        c,_=lib.sync(self.root,self.state)
        card=copy.deepcopy(self.card)
        card.update(id='NEW',source='new.pptx',source_hashes={'new.pptx':c['sources']['new.pptx']['sha256']})
        card['evidence'][0]['source']='new.pptx'
        lib.record_analysis(card,c,self.state)
        self.assertTrue(any(p.get('id')=='NEW' and p['status']=='needs_language_analysis' for p in self.command('pending')['pending']))
        profile=copy.deepcopy(self.profile)
        profile.update(id='NEW',source='new.pptx',source_sha256=c['sources']['new.pptx']['sha256'])
        profile['expressions'][0]['id']='NEW-E01'
        input_file=Path(self.temp.name)/'new-language.json'
        input_file.write_text(json.dumps(profile), encoding='utf-8')
        saved=self.command('record-language',str(input_file))
        self.assertEqual(saved['pending_count'],0)
        result=self.command('phrases','--id','NEW','--role','문제 제기')
        self.assertEqual(result['match_count'],1)
        self.assertEqual(result['matches'][0]['source'],'new.pptx')

    def test_metrics_do_not_count_note_boilerplate_as_script(self):
        source={'units':[{'slide':1,'notes':'발표문 초안\n첫 문장입니다. 다음 질문은 무엇일까요.\n자료: 원본.pdf 1쪽\nhttps://example.com'}]}
        metrics=lang.metrics(source)
        self.assertEqual(metrics['sentence_count'],2)
        self.assertEqual(metrics['question_ending_sentences'],1)


if __name__ == '__main__':
    unittest.main()
