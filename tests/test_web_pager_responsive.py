"""Synthetic web article pagination at narrow widths; no external site or mail."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('language,width', [('es', 280), ('es', 320), ('en', 320), ('en', 390)])
def test_web_pager_fits_and_navigates_with_large_text(engine, language, width):
    with browser_fixture(language, {'width': width, 'height': 640}, engine=engine, touch=True) as (page, errors):
        page.evaluate('''() => {
          const original=window.webxdc.sendUpdate.bind(window.webxdc);
          window.webxdc.sendUpdate=(update,info)=>{
            const req=update && update.payload;
            if (!req || req.op !== 'read') return original(update,info);
            setTimeout(()=>window.__listener({serial:++window.__serial,payload:{t:'res',re:req.id,ok:true,
              data:{url:req.args.url,title:'Synthetic article',page:req.args.page || 1,pages:4567,nav:[],media:false,
                html:'<p>Synthetic offline response.</p>'}}}),0);
          };
          go('reader',{url:'https://fixture.example/article'});
        }''')
        page.locator('.pager').wait_for()
        page.add_style_tag(content='.pager .btn, .pager span {font-size: 18px !important}')
        assert page.locator('.reader').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        assert page.locator('#view').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        assert page.evaluate('''() => {const p=document.querySelector('.pager'), box=p.getBoundingClientRect();
          const nodes=[...p.querySelectorAll('button,span')], rects=nodes.map(n=>n.getBoundingClientRect());
          return nodes.every((n,i)=>rects[i].left>=box.left-1 && rects[i].right<=box.right+1 &&
            (n.tagName!=='BUTTON' || rects[i].height>=44)) && rects.every((r,i)=>rects.every((s,j)=>i===j ||
            r.right<=s.left+1 || s.right<=r.left+1 || r.bottom<=s.top+1 || s.bottom<=r.top+1));}''')
        next_label, previous_label = ('Siguiente', 'Anterior') if language == 'es' else ('Next', 'Previous')
        pager = page.locator('.pager')
        assert pager.get_by_role('button', name=previous_label).is_disabled()
        pager.get_by_role('button', name=next_label).tap()
        page.wait_for_function("() => /(?:Página|Page) 2 /.test(document.querySelector('.pager span')?.textContent || '')")
        page.locator('.pager').get_by_role('button', name=previous_label).tap()
        page.wait_for_function("() => /(?:Página|Page) 1 /.test(document.querySelector('.pager span')?.textContent || '')")
        assert errors == []
