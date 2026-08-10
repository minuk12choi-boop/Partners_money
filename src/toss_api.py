#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toss_api.py — 쉐어링크 대시보드의 상품 목록에서 가격 정보를 읽는다

**왜 필요한가**

토스 앱에서 쉐어링크를 공유하면 클립보드에 상품명과 링크만 들어온다.
가격이 없다. `toss.shopping/t/<id>` 웹 페이지도 가격을 안 준다 —
화면에도 없고 HTML 에도 없다(실측: `displayPrice` 0회).

그런데 쉐어링크 대시보드가 목록을 그릴 때 부르는 API 에는 다 있다.

    GET /api-public/v3/shopping/sharelink/products?size=120
    items[].taca.productView = {
        tacaId, displayName, originalPrice, displayPrice, discountRate,
        reviewScore, reviewCount, isSoldOut, ...
    }
    items[].taca.displayContext.isLowestPriceIn30Days
    items[].hasMinPrice30dBadge

**`tacaId` 가 `toss.shopping/t/<id>` 의 id 다.** 그래서 쉐어링크를 해석해
얻은 상품 ID 로 이 목록에서 찾을 수 있다.

────────────────────────────────────────────────────────────────
🔴 `/products/{id}` 를 쓰지 말 것 — 조용히 다른 상품을 준다

    요청: /products/2387171175   (맥스앤맥스 밀폐용기의 tacaId)
    응답: 200 SUCCESS
          tacaItemId 2387171175, displayName "메이빈 팰리세이드 대시보드커버"

이 엔드포인트는 `tacaId` 가 아니라 **`tacaItemId`** 로 조회한다. 두 값이
우연히 겹치는 다른 상품이 있어서, 404 도 아니고 성공 응답으로 엉뚱한
상품을 돌려준다. 그대로 쓰면 **틀린 가격으로 발행하게 된다.**
목록에서 `tacaId` 로 찾는 방식만 쓴다.
────────────────────────────────────────────────────────────────

**한계**: 이 목록은 대시보드가 큐레이션한 117개뿐이다. 딜방에서 온 임의
상품은 대부분 여기에 없다. 없으면 가격을 못 얻는다. 그때는 딜방 메시지에
적힌 가격을 쓰거나(그쪽에 있다) 사장님이 직접 적어 주셔야 한다.
`size` 를 500 으로 올려도 117개 그대로다(실측).
"""

import json

PRODUCTS_API = ("https://sharelink.toss.im/api-public/v3/shopping/"
                "sharelink/products?size=120")

# 페이지 안에서 부른다. 브라우저의 로그인 세션을 그대로 쓴다.
_JS_FETCH = """async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    return { status: r.status, text: await r.text() };
  } catch (e) { return { status: -1, text: String(e) }; }
}"""


def fetch_products(page, log=print):
    """{tacaId(str): 상품정보} 를 돌려준다. 실패하면 빈 dict.

    `page` 는 sharelink.toss.im 에 로그인된 Playwright 페이지여야 한다.
    같은 출처에서 불러야 쿠키가 실린다.
    """
    try:
        r = page.evaluate(_JS_FETCH, PRODUCTS_API)
    except Exception as e:
        log(f"  상품 목록 조회 실패: {e}")
        return {}
    if r.get("status") != 200:
        log(f"  상품 목록 조회 실패: HTTP {r.get('status')}")
        return {}
    try:
        data = json.loads(r["text"])
    except Exception as e:
        log(f"  상품 목록 파싱 실패: {e}")
        return {}
    if data.get("resultType") != "SUCCESS":
        log(f"  상품 목록 오류: {(data.get('error') or {}).get('reason')}")
        return {}

    out = {}
    for it in (data.get("success") or {}).get("items") or []:
        taca = it.get("taca") or {}
        pv = taca.get("productView") or {}
        tid = pv.get("tacaId")
        if tid is None:
            continue
        ctx = taca.get("displayContext") or {}
        out[str(tid)] = {
            "taca_id": str(tid),
            "title": pv.get("displayName"),
            "price": pv.get("displayPrice"),
            "original_price": pv.get("originalPrice"),
            "discount_pct": pv.get("discountRate"),
            "rating": pv.get("reviewScore"),
            "reviews": pv.get("reviewCount"),
            "sold_out": pv.get("isSoldOut"),
            "lowest30": bool(ctx.get("isLowestPriceIn30Days")
                             or it.get("hasMinPrice30dBadge")),
            "category": it.get("categoryName"),
        }
    return out


def lookup(page, taca_id, log=print, cache=None):
    """상품 ID 하나를 목록에서 찾는다. 없으면 None.

    `cache` 에 dict 를 주면 목록을 거기에 담아 재사용한다. 한 번 실행에서
    여러 건을 볼 때 API 를 반복해서 부르지 않기 위함이다.
    """
    if cache is not None and cache:
        products = cache
    else:
        products = fetch_products(page, log=log)
        if cache is not None:
            cache.update(products)
    return products.get(str(taca_id))
