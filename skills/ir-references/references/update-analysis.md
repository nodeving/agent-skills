# 추가·변경된 자료 분석

## 자동 확인과 분석 작성

도구의 `sync`·검색·조회는 매번 원본 파일의 SHA-256을 비교한다. 새 파일, 같은 이름의 수정, 삭제를 감지한다. 원본이 바뀐 분석은 `needs_review`가 되고 기본 검색에서 제외된다. 원본 삭제는 현재 검색에서 제외한다. **파일 해시 갱신만으로 AI 분석을 완료했다고 표시하지 않는다.**

1. `sync`와 `pending`으로 대상과 추출 오류를 확인한다. 지원 형식은 PPTX·MD·TXT·XLSX·PDF다. ZIP 및 다른 형식은 `ignored_files`에 표시된다. ZIP을 자동으로 풀거나 삭제된 자료의 복구 수단으로 쓰지 않는다.
2. 새 PPTX는 `source "상대 경로" --offset 0 --limit 5`로 순서와 노트를 묶어 읽는다. 이어지는 구간을 읽어 전체 발표의 질문·해결·검증·사업화·마무리를 파악한다. 고정된 목차에 억지로 맞추지 않는다.
3. 핵심 전환, 대표 장점, 화면과 노트의 불일치가 의심되는 슬라이드를 시각 확인한다. `images`가 추출한 이미지를 직접 열거나 해당 PPT를 렌더링한다. 한 장씩 노트와 맞춰 화면의 주장, 말이 더하는 맥락, 다음 장으로의 연결을 분석한다. 텍스트 없는 PDF도 필요한 페이지를 시각 확인한다.
4. 기존 분석 문서·매칭표가 관련되면 해당 구간만 읽는다. 이를 사용한 해석에는 출처와 범위를 남긴다. 기존 분석과 현재 PPT의 차이는 현재 파일을 근거로 기록한다.
5. 아래 구조로 **내용을 직접 분석해** JSON 파일을 작성한다. `source_hashes`에는 실제로 읽은 파일의 당시 해시를 사용한다. `source` 명령이 해시를 반환한다. 읽은 후 원본이 바뀌면 다시 검토한다.
6. `record 분석파일.json`으로 구조 분석을 저장한다. **PPTX는 이어서 [어투·표현 분석 절차](language-analysis.md)에 따라 노트 전체의 어투와 좋은 원문 표현을 분석하고 `record-language`로 저장한다.** 구조 카드만 저장하면 `needs_language_analysis`가 남는다.
7. `pending`으로 구조·어투·표현에서 남은 대상을 확인하고 `report`로 보고서 3종을 갱신한다. 좋은 표현은 원문 그대로 저장하며 검증을 통과해야 한다.

자료가 추가되면 새 카드만 작성한다. 자료가 수정되면 그것을 근거로 쓴 카드와 관련 전환을 재검토한다. 삭제된 주 자료의 카드는 현재 참고 목록에서 제외하며 자동으로 다른 원본에 연결하지 않는다. 단지 생성 시각이 최신이라는 이유로 더 좋은 참고나 최종본으로 취급하지 않는다.

PPT 원문이 바뀌면 해당 어투·표현 데이터도 재검토 대상이 되고 `tone`·`phrases`의 현재 결과에서 제외된다. 노트가 수정된 장뿐 아니라 앞뒤 전환과 문체의 범위를 확인한다. `source_sha256`만 새 값으로 덮어쓰지 않는다. 삭제된 PPT의 표현은 현재 검색·표현집에서 빠진다.

추출 오류는 결과의 `extraction_errors`에 나온다. 읽지 못한 파일을 이전 텍스트로 대체하거나 분석 완료로 표시하지 않는다. PDF 텍스트 추출에는 `pdftotext`가 필요하며, 이미지 PDF는 별도 시각 검토가 필요하다.

## 분석 카드 구조

`id`는 검색에 사용할 짧고 고유한 이름이다. 영문·숫자·밑줄·하이픈을 사용할 수 있다. 자료가 삭제되어도 기존 ID를 다른 자료에 재사용하지 않는다.

아래는 PPTX용 구조다. MD·TXT·PDF·XLSX 카드에서는 `script_analysis`와 PPTX 슬라이드 전용 `inspected_slides`를 생략하고, 근거 위치를 실제 문서 줄·페이지·시트 행으로 기록한다. `script_analysis`를 다른 형식에 남기면 저장이 거절된다.

```json
{
  "id": "NEW01",
  "title": "회사 또는 자료 이름",
  "source": "PPT 발표문/새 자료.pptx",
  "source_hashes": {"PPT 발표문/새 자료.pptx": "읽을 때 확인한 SHA-256"},
  "summary": "이 자료에서 재사용할 핵심 연결 방식",
  "flow": "p1–2의 역할 → p3의 역할 → 이후 구간의 역할",
  "strengths": ["구체적 장점과 이를 뒷받침하는 화면·노트 구성"],
  "strategy": "그 구성이 청중의 어떤 의문을 어떻게 해소하는지",
  "application": "어떤 상황에 적합하고 무엇이 갖춰져야 적용할 수 있는지",
  "script_use": "화면과 말의 분업을 새 원고에 적용하는 방법",
  "script_analysis": {
    "matched_slides": [2, 3],
    "screen_voice_relation": "각 장의 화면 내용과 발표자 노트를 구체적으로 대조한 해석",
    "effective_connection": "앞 장의 질문을 다음 장과 노트가 해소하는 방식",
    "improvement": "불일치·낭독·비약 또는 전달을 보강할 실제 지점"
  },
  "limitations": ["발췌본 여부·실적/목표·확인 범위 등 이 자료의 구체적인 한계"],
  "evidence": [
    {"source": "PPT 발표문/새 자료.pptx", "locator": "slide 2", "observation": "직접 확인한 화면과 해당 노트의 구체적 내용"}
  ],
  "tags": ["고객 여정", "유료 검증"],
  "review_scope": "전체 노트를 읽은 범위와 직접 본 화면, 기존 분석을 인용한 범위를 구분",
  "inspected_slides": [2, 3]
}
```

PPTX 카드에는 `script_analysis`가 필수다. 자료에 발표자 노트가 없다면 없다고 기록하고 실제 대본처럼 생성하지 않는다. `matched_slides`에는 화면·노트 관계를 검토한 장을, `inspected_slides`에는 직접 이미지를 본 장만 기록한다. 원문 전체는 카탈로그에서 불러오므로 카드에 복사하지 않아도 된다.

스킬 폴더에서 실행하는 예시다. `IR_LIBRARY_ROOT`를 사용자가 지정한 자료 폴더로 설정하고, JSON 경로는 직접 분석해 작성한 파일로 바꾼다. 별도 `--state`를 사용했다면 모든 명령에 동일하게 추가한다.

```bash
python3 scripts/ir_library.py --root "$IR_LIBRARY_ROOT" record new-analysis.json
python3 scripts/ir_library.py --root "$IR_LIBRARY_ROOT" record-language new-language-analysis.json
python3 scripts/ir_library.py --root "$IR_LIBRARY_ROOT" pending
python3 scripts/ir_library.py --root "$IR_LIBRARY_ROOT" report
```

읽기용 보고서는 원본이 아니라 카드로부터 생성되는 결과다. 카드에 수정하고 다시 생성한다. 새 발표 자료는 사용자가 원하는 위치에 저장하며, 결과물을 레퍼런스 폴더에 자동으로 되먹임하지 않는다.
