"""Mobile-first browser checks with synthetic mail and public catalogue fixtures."""
import pytest
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine,width', [('chromium', 320), ('webkit', 390)])
def test_black_rescue_cat_matches_profile_and_scroll_is_discoverable(engine, width):
    with browser_fixture('es', {'width': width, 'height': 844}, engine=engine) as (page, errors):
        page.wait_for_function('cache.at > 0 && viewReady()')
        assert page.locator('.home-brand-mark img').get_attribute('src').startswith('data:image/png;base64,')
        assert page.locator('.mascot-seated .mascot-glasses').count() == 1
        assert '#f5b846' in page.locator('.mascot-seated').inner_html()
        assert '#eaaa68' not in page.locator('.mascot-seated').inner_html()
        assert page.locator('.mascot-seated .mascot-glasses circle').first.evaluate('n=>getComputedStyle(n).fill') == 'none'
        cue = page.locator('#scroll-discovery')
        cue.wait_for(state='visible')
        before = page.locator('#view').evaluate('node=>node.scrollTop')
        cue.click()
        page.wait_for_function('document.querySelector("#view").scrollTop > ' + str(before))
        page.evaluate('()=>{const v=document.querySelector("#view");v.scrollTop=v.scrollHeight}')
        cue.wait_for(state='hidden')
        page.evaluate("goTab('ai')")
        cue.wait_for(state='visible')
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_nested_disclosure_arrows_follow_only_own_state(engine):
    with browser_fixture('en', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate('''() => {
          const inner=h('details',{id:'inner'},h('summary',{},'Inner',icon('down','caret')),h('p',{},'More'));
          const outer=h('details',{id:'outer',open:true},h('summary',{},'Outer',icon('down','caret')),inner);
          document.querySelector('#view').replaceChildren(outer);
        }''')
        assert page.locator('#inner > summary > .caret').evaluate('n=>getComputedStyle(n).transform') == 'none'
        page.locator('#inner > summary').click()
        page.wait_for_function("getComputedStyle(document.querySelector('#inner > summary > .caret')).transform !== 'none'")
        assert errors == []


def test_pending_sheet_is_bounded_and_diagnostics_are_folded():
    with browser_fixture('es', {'width': 320, 'height': 844}) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.evaluate('''()=>{window.__delay=60000;for(let i=0;i<16;i++)bridge.request('health',{i});pendingSheet()}''')
        assert page.locator('.pending-compact-row').count() == 6
        assert not page.locator('.pending-diagnostics').evaluate('n=>n.open')
        page.get_by_role('button', name='Mostrar más solicitudes', exact=True).click()
        assert page.locator('.pending-compact-row').count() == 12
        assert page.locator('.pending-compact-list').evaluate('n=>n.clientHeight <= innerHeight*.54')
        assert errors == []


def test_image_results_stay_near_prompt_and_models_have_real_marks():
    with browser_fixture('es', {'width': 320, 'height': 844}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate('''()=>{cache.models.images=[{key:'cf:flux-1-schnell',label:'FLUX.1 schnell',used:0,daily:10}];go('images')}''')
        picker = page.locator('.image-model-picker')
        assert not picker.evaluate('n=>n.open')
        assert page.locator('.gallery').bounding_box()['y'] < 740
        picker.locator(':scope > summary').click()
        page.locator('.image-provider > summary').click()
        page.locator('.image-family > summary').click()
        assert page.locator('.image-family .model-choice .brand-official img').count() == 1
        assert page.locator('.image-provider > summary .brand-official img').count() == 1
        assert page.locator('#view').evaluate('n=>n.scrollWidth<=n.clientWidth+1')
        assert errors == []


def test_library_download_persists_and_reopens_without_network():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate('''()=>{const original=bridge.call.bind(bridge);bridge.call=async(op,args)=>{
          if(op!=='books')return original(op,args);
          if(args.do==='search')return {books:[{id:'fixture:1',source:'Fixture',title:'Un libro completo',authors:['Autor'],url:'https://example.org/book',downloads:[{format:'txt',url:'https://example.org/book.txt'}]}],more:false};
          if(args.do==='download')return {text:'Primera página.\\n\\nContenido completo del libro.',complete:true,sha256:'fixture',source_format:'txt'};
          return {};
        };go('books')}''')
        page.get_by_role('heading', name='Un libro completo', exact=True).wait_for(state='visible')
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.get_by_role('button', name='Abrir', exact=True).wait_for(state='visible')
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 1
        page.get_by_role('button', name='Abrir', exact=True).click()
        assert page.locator('.book-reading').inner_text().startswith('Primera página.')
        assert page.get_by_role('button', name='Exportar al lector del sistema', exact=True).is_visible()
        page.reload()
        page.wait_for_function('cache.at > 0')
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).wait_for(state='visible')
        assert page.locator('.book-card').count() == 1
        assert errors == []


def test_local_utilities_never_send_text_to_server():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.evaluate("go('tools')")
        before = page.evaluate('window.__sentUpdates.length')
        row = page.locator('.local-utilities details').filter(has=page.get_by_text('Formatear JSON', exact=True))
        row.locator('summary').click()
        row.locator('textarea').fill('{"private":"device only"}')
        row.get_by_role('button', name='Usar herramienta', exact=True).click()
        assert 'device only' in row.locator('pre').inner_text()
        assert page.evaluate('window.__sentUpdates.length') == before
        assert errors == []


def test_api_presets_and_donation_have_separate_explicit_consent():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate('''()=>{const original=bridge.call.bind(bridge);window.__donationCalls=[];bridge.call=async(op,args)=>{
          if(op!=='personal_ai')return original(op,args);
          window.__donationCalls.push(args);
          return {providers:[{id:'personal-test',name:'Mi proveedor',base:'https://example.org/v1',kind:'openai',models:[],daily:20,attempts:0}]};
        };go('personal_ai')}''')
        page.locator('select').first.select_option('groq')
        assert page.locator('input[type=url]').input_value() == 'https://api.groq.com/openai/v1'
        assert not page.locator('.personal-advanced').evaluate('n=>n.open')
        page.get_by_role('button', name='Aportar API · opcional', exact=True).click()
        submit = page.get_by_role('button', name='Donar voluntariamente', exact=True)
        assert submit.is_disabled()
        assert not any(call.get('do') == 'donate' for call in page.evaluate('window.__donationCalls'))
        page.locator('.sheet input[type=checkbox]').check()
        assert submit.is_enabled()
        assert errors == []
