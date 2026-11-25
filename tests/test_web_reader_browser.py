"""Current, unchanged WebXDC UI against a synthetic reader backend.

The WebXDC transport is emulated; this does not test a native client or mail.
"""
from __future__ import annotations

import io
import json
from unittest import mock

from PIL import Image
import pytest

from michiflin import browser, links, media, miniapp, net, reader, state, tools
from tests.test_webxdc_browser import browser_fixture, screenshot_if_requested


class Response:
    def __init__(self, body, url, kind):
        self.body, self.url = body, url
        self.headers = {'content-type': kind}
        self.ok, self.status_code, self.closed = True, 200, False

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


INTERCEPT = '''() => {
  const native = window.webxdc.sendUpdate.bind(window.webxdc);
  window.__readerFixtureRequests = []; window.__readerFixtureChunks = [];
  window.webxdc.sendUpdate = (update, info) => {
    const request = update && update.payload;
    if (!request || request.t !== 'req' || !['search','read'].includes(request.op)) return native(update, info);
    window.__readerFixtureRequests.push({op:request.op,args:request.args});
    fetch('/api/delivery', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)})
      .then(response => response.json()).then(body => {
        if (body.error) throw Error(body.error);
        for (const payload of body.data.updates) {
          if (payload.t === 'chunk') window.__readerFixtureChunks.push(new Blob([JSON.stringify({payload})]).size);
          window.__listener({serial:++window.__serial,payload});
        }
      }).catch(error => { window.__readerFixtureError = String(error); });
  };
}'''


@pytest.mark.parametrize('engine,width', [('chromium', 320), ('webkit', 390)])
def test_search_result_reader_picture_pagination_and_navigation(monkeypatch, engine, width):
    url, related = 'https://fixture.example/story', 'https://fixture.example/related'
    settings = {'reader_images': 'low', 'reader_links': 'chat', 'reader_kb': 30}
    monkeypatch.setattr(state, 'setting', lambda key: settings[key])
    monkeypatch.setattr(reader, '_cache', {})
    monkeypatch.setattr(miniapp, '_answers', {})
    monkeypatch.setattr(browser, 'available', lambda: False)
    monkeypatch.setattr(media, 'available', lambda: False)
    monkeypatch.setattr(net, 'validate_public_url', lambda value: value)
    monkeypatch.setattr(links, '_bot_address', 'bot@example.invalid')
    monkeypatch.setattr(tools, 'search', lambda query, locale, limit: [
        tools.Result('Guía del jardín', url, 'Una guía sintética con fotografías y consejos.', 'Revista local')])
    picture = io.BytesIO()
    Image.new('RGB', (300, 160), (65, 144, 112)).save(picture, 'PNG')
    source = ('''<html><head><title>Guía del jardín</title><script>window.attack=1</script></head>
       <body><header><nav><a href="/home">Inicio</a></nav></header><article>
       <header><h1>Guía del jardín</h1><p>Por Ana · 2026</p></header>
       <p>Una introducción para la lectura en móvil. <a href="/related">Saber más</a>.</p>
       <figure><picture><source srcset="/garden.png"><img src="data:image/gif;base64,AA==" alt="Jardín"></picture>
       <figcaption>Un jardín de ejemplo</figcaption></figure><p>'''
       + ('Flores y plantas para una lectura cómoda. ' * 580)
       + '</p><p>FINAL SEGUNDA PÁGINA ' + ('Más información. ' * 300) + '</p></article></body></html>')
    related_html = '<title>Artículo relacionado</title><article><p>Lectura relacionada.</p></article>'
    fetched, replies = [], []

    def fetch(target, **kwargs):
        fetched.append((target, kwargs.get('anonymous', False)))
        if target == 'https://fixture.example/garden.png':
            assert kwargs['anonymous'] is True
            return Response(picture.getvalue(), target, 'image/png')
        return Response((source if target == url else related_html).encode(), target, 'text/html')

    monkeypatch.setattr(net, 'fetch', fetch)

    def handler(request):
        data = miniapp.OPS[request['op']](7, request['args'], request.get('lang', 'es'))
        updates = []
        with mock.patch.object(miniapp, '_status_allowed', return_value=True), \
             mock.patch.object(miniapp, '_send_status', side_effect=lambda _instance, value: updates.append(value) or True):
            miniapp.respond(7, request['id'], data)
        replies.append(data)
        return {'updates': updates}

    with browser_fixture('es', {'width': width, 'height': 844}, engine=engine, delivery_handler=handler) as (page, errors):
        page.evaluate(INTERCEPT)
        page.locator('.tab[data-tab="web"]:visible').click()
        query = page.get_by_placeholder('Buscar o pegar un enlace')
        query.fill('consejos de jardín')
        query.press('Enter')
        result = page.locator('.result').filter(has_text='Guía del jardín')
        result.wait_for()
        assert result.get_by_text('Una guía sintética con fotografías y consejos.').is_visible()
        assert result.get_by_role('button', name='Abrir en Live').is_visible()
        screenshot_if_requested(page, f'web-busqueda-{engine}-{width}')
        result.locator('.result-main').click()
        page.locator('.page-body figure img').wait_for()
        assert page.locator('.page-body figure img').get_attribute('src').startswith('data:image/jpeg;base64,')
        assert page.locator('.page-body figcaption').inner_text() == 'Un jardín de ejemplo'
        assert page.locator('.page-body').get_by_text('Por Ana · 2026').is_visible()
        assert page.locator('.page-body script, .page-body svg').count() == 0
        assert page.locator('.pager').get_by_text('Página 1 de 2').is_visible()
        screenshot_if_requested(page, f'web-lector-{engine}-{width}')
        assert page.locator('.page-body').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
        overflow = page.locator('.reader').evaluate('''el => ({width:el.clientWidth, scroll:el.scrollWidth,
          outside:[...el.querySelectorAll('*')].filter(x => x.getBoundingClientRect().right > el.getBoundingClientRect().right + 1)
            .map(x => ({tag:x.tagName,inPager:!!x.closest('.pager')}))})''')
        assert overflow['scroll'] <= overflow['width'] + 1, overflow
        page.locator('.pager').get_by_role('button', name='Siguiente').click()
        page.locator('.page-body').get_by_text('FINAL SEGUNDA PÁGINA', exact=False).wait_for()
        assert page.locator('.pager').get_by_text('Página 2 de 2').is_visible()
        page.locator('.pager').get_by_role('button', name='Anterior').click()
        page.locator('.page-body').get_by_text('Saber más').click()
        page.locator('.page-body').get_by_text('Lectura relacionada.').wait_for()
        assert fetched.count((url, False)) == 1  # page 2 reused the same contact-scoped fetch
        assert fetched.count(('https://fixture.example/garden.png', True)) == 1
        assert fetched.count((related, False)) == 1
        assert all(len(json.dumps(reply, ensure_ascii=False).encode()) < 128000 for reply in replies)
        assert all(size < 128000 for size in page.evaluate('window.__readerFixtureChunks'))
        assert not page.evaluate('window.__readerFixtureError || false') and errors == []


def test_search_limit_is_visible_in_the_existing_ui_instead_of_no_results(monkeypatch):
    attempts = []
    def challenged(query, _locale, _limit):
        attempts.append(query)
        tools._pause_search()
        return []
    monkeypatch.setattr(tools, '_free_search', challenged)
    monkeypatch.setattr(tools, '_cache', {})
    monkeypatch.setattr(tools, '_search_paused_until', 0.0)
    def handler(request):
        return miniapp.op_search(7, request['args'], 'es')
    with browser_fixture('es', {'width': 390, 'height': 844}, delivery_handler=handler) as (page, errors):
        page.evaluate('''() => {
          const native=window.webxdc.sendUpdate.bind(window.webxdc);
          window.webxdc.sendUpdate=(update,info)=>{
            const req=update && update.payload;
            if (!req || req.t !== 'req' || req.op !== 'search') return native(update,info);
            fetch('/api/delivery',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(req)})
              .then(response=>response.json()).then(body=>window.__listener({serial:++window.__serial,
                payload:{t:'res',re:req.id,ok:!body.error,data:body.data,error:body.error}}));
          };
        }''')
        page.locator('.tab[data-tab="web"]:visible').click()
        query = page.get_by_placeholder('Buscar o pegar un enlace')
        query.fill('consulta sintética')
        query.press('Enter')
        page.locator('.failure').get_by_text('El buscador limita las solicitudes', exact=False).wait_for()
        assert page.get_by_text('Sin resultados. Prueba con otras palabras.').count() == 0
        assert attempts == ['consulta sintética'] and errors == []


def test_candidate_app_pager_fits_a_320px_screen():
    with browser_fixture('es', {'width': 320, 'height': 844}, engine='chromium') as (page, errors):
        page.evaluate('''() => {
          const original=window.webxdc.sendUpdate.bind(window.webxdc);
          window.webxdc.sendUpdate=(update,info)=>{
            const req=update && update.payload;
            if (!req || req.op !== 'read') return original(update,info);
            setTimeout(()=>window.__listener({serial:++window.__serial,payload:{t:'res',re:req.id,ok:true,
              data:{url:req.args.url,title:'Artículo',page:1,pages:2,nav:[],media:false,html:'<p>Prueba.</p>'}}}),0);
          };
          go('reader',{url:'https://fixture.example/a'});
        }''')
        page.locator('.pager').wait_for()
        assert page.locator('.reader').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
        assert errors == []
