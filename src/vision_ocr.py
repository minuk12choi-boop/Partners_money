#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vision_ocr.py — 상품 스크린샷에서 상품명·가격·정가·할인율을 읽는다

**왜 필요한가**

토스는 웹에서 가격을 완전히 뺐다(실측: 상품 페이지·카테고리 랭킹·홈 어디에도
'원' 붙은 숫자가 0개, RSC 페이로드와 script 전체에도 없음). 그래서 링크만으로는
가격을 얻을 수 없는 상품이 있다.

그런데 **앱 화면에는 다 보인다.** 스크린샷 한 장에 상품명, 정가, 판매가,
할인율, 평점이 전부 들어 있다. 그걸 읽는다.

정확도가 중요하다. 가격을 잘못 읽으면 틀린 가격으로 발행된다. 그래서
구조화 출력(json_schema)으로 형식을 강제하고, 읽지 못한 값은 지어내지 말고
null 로 두게 한다.

**설정**

    ANTHROPIC_API_KEY=sk-ant-...     # .env 또는 시스템 환경변수

**비용** (claude-opus-5, 입력 $5 / 출력 $25 per MTok)

    폰 스크린샷 1장 ≈ 3,300 입력 토큰 + 150 출력 토큰
    ≈ $0.021 ≈ 30원/장

    하루 50장이면 월 약 45,000원이다. 적은 돈이 아니다.
    OCR_MODEL 을 바꿔 더 싼 모델로 내릴 수 있지만 한글 인식률이 떨어진다.
"""

import base64
import json
import os

from env import load_env

load_env()

# 기본값은 가장 정확한 모델이다. 가격을 잘못 읽으면 틀린 가격이 발행되므로
# 여기서 아끼면 안 된다. 바꾸려면 .env 에 OCR_MODEL 을 넣는다.
OCR_MODEL = os.environ.get("OCR_MODEL", "claude-opus-5")

MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
}

# 읽지 못한 값은 null 이어야 한다. 지어내면 틀린 가격이 나간다.
SCHEMA = {
    "type": "object",
    "properties": {
        "platform": {
            "type": ["string", "null"],
            "enum": ["toss", "coupang", None],
            "description": "화면이 토스쇼핑이면 toss, 쿠팡이면 coupang, 모르겠으면 null",
        },
        "title": {
            "type": ["string", "null"],
            "description": "상품명 전체. 옵션·용량·수량까지 그대로. 없으면 null",
        },
        "price": {
            "type": ["integer", "null"],
            "description": "실제 판매가(원). 가장 크게 강조된 금액. 없으면 null",
        },
        "original_price": {
            "type": ["integer", "null"],
            "description": "정가(원). 보통 취소선이 그어진 금액. 없으면 null",
        },
        "discount_pct": {
            "type": ["integer", "null"],
            "description": "할인율(%). 화면에 적힌 숫자 그대로. 없으면 null",
        },
        "rating": {
            "type": ["number", "null"],
            "description": "평점. 없으면 null",
        },
        "reviews": {
            "type": ["integer", "null"],
            "description": "리뷰 개수. 없으면 null",
        },
        "note": {
            "type": ["string", "null"],
            "description": "읽지 못한 항목이나 애매했던 점. 없으면 null",
        },
    },
    "required": ["platform", "title", "price", "original_price",
                 "discount_pct", "rating", "reviews", "note"],
    "additionalProperties": False,
}

PROMPT = """이 이미지는 쇼핑 앱의 상품 화면 스크린샷입니다.
상품명과 가격 정보를 정확히 읽어 주세요.

규칙:
- 화면에 **실제로 보이는 값만** 적습니다. 추측하거나 계산하지 마세요.
- 판매가는 가장 크고 강조된 금액입니다.
- 정가는 보통 취소선이 그어진 금액입니다. 없으면 null 입니다.
- 할인율은 화면에 적힌 % 숫자 그대로입니다. 직접 계산하지 마세요.
- '1팩 당 400원' 같은 단위당 단가는 판매가가 아닙니다. 무시하세요.
- '최대 693원 적립' 같은 적립 금액도 가격이 아닙니다. 무시하세요.
- 상품명은 줄바꿈 없이 한 줄로, 옵션·용량·수량까지 그대로 옮깁니다.
- 읽을 수 없는 항목은 반드시 null 로 두세요. 지어내면 틀린 가격이 발행됩니다."""


class OcrUnavailable(RuntimeError):
    """API 키가 없거나 SDK 가 설치되지 않았다."""


def media_type_for(path):
    ext = os.path.splitext(path)[1].lower()
    return MEDIA_TYPES.get(ext, "image/jpeg")


def read_screenshot(image_bytes, media_type="image/jpeg", log=print):
    """스크린샷에서 상품 정보를 읽는다. dict 를 돌려준다.

    실패하면 OcrUnavailable 또는 원래 예외를 올린다. 조용히 None 을
    돌려주지 않는다 — 무인 운영에서 조용한 실패가 가장 나쁘다.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise OcrUnavailable(
            "ANTHROPIC_API_KEY 가 없습니다. `.env` 에 넣어 주세요.")
    try:
        import anthropic
    except ImportError:
        raise OcrUnavailable(
            "anthropic 패키지가 없습니다. `py -m pip install -r requirements.txt`")

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=OCR_MODEL,
        max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image_bytes).decode(),
                }},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )

    if resp.stop_reason == "refusal":
        raise RuntimeError("이미지 판독이 거부되었습니다.")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("응답에 본문이 없습니다.")

    data = json.loads(text)
    u = resp.usage
    log(f"  OCR: 입력 {u.input_tokens} / 출력 {u.output_tokens} 토큰")
    return data


def main():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="상품 스크린샷 판독 시험")
    ap.add_argument("image", help="이미지 파일 경로")
    args = ap.parse_args()

    with open(args.image, "rb") as f:
        data = read_screenshot(f.read(), media_type_for(args.image))
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
