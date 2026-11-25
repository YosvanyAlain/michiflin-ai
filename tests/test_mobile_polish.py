"""Second owner review: compact UI, explicit group approval, no live mail."""
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from michiflin import app_compat, book_library, config, group_tools, miniapp, state
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine,width', [('chromium', 320), ('webkit', 390), ('chromium', 1280)])
def test_direct_home_settings_and_separate_tools(engine, width):
    with browser_fixture('es', {'width': width, 'height': 844}, engine=engine) as (page, errors):
        assert page.locator('#splash,.intro-overlay,.mascot,.home-brand-mark').count() == 0
        assert page.locator('.home-banner-slot').bounding_box()['height'] <= 210
        cue = page.locator('#scroll-discovery')
        if cue.is_visible():
            rect = cue.bounding_box()
            assert rect['x'] > width * .8 and rect['width'] <= 44
            assert cue.get_attribute('aria-label')
        page.evaluate("go('settings')")
        assert page.locator('.settings-tile').count() == 9
        for section in ['appearance', 'ai', 'navigation', 'bot', 'privacy', 'library', 'connection', 'support']:
            page.evaluate("section=>go('settings',{section})", section)
            assert page.locator('#view h2').count() >= 1
            assert page.locator('#view').evaluate('el=>el.scrollWidth <= el.clientWidth+1')
        page.evaluate("go('tools')")
        assert page.locator('.utility-tile').count() == 10
        assert page.locator('.utility-card,.local-tool-card').count() == 0
        page.locator('.utility-tile').first.click()
        assert page.locator('.utility-workspace').count() == 1
        assert page.locator('.local-tool-card details').count() == 0
        assert errors == []


def install_books(page):
    page.evaluate('''() => {
      const original=bridge.call.bind(bridge); window.bookCalls=[];
      window.sampleBooks=[{id:'available',title:'Libro descargable',authors:['Autor'],source:'Fixture',downloads:[{format:'txt',url:'https://example.org/book.txt'}]},
        {id:'loan',title:'Solo préstamo',authors:[],source:'Fixture',downloads:[]}];
      bridge.call=(op,args)=>{
        if(op!=='books')return original(op,args);
        window.bookCalls.push(args);
        if(args.do==='search')return Promise.resolve({books:window.sampleBooks,more:false});
        if(args.do==='original_start')return Promise.resolve({transfer:'synthetic',format:'txt',size:15,count:1,index:0,
          sha256:'3842f60d8f4454e5f17a426eac12ba5069a08a5f68b96be391485bd67660cc59',data:btoa('Texto completo.')});
        return Promise.resolve({});
      };go('books',{q:'libro'});
    }''')


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_books_download_only_compact_and_reopen_with_fresh_search(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        install_books(page)
        page.get_by_text('Libro descargable', exact=True).wait_for()
        assert page.get_by_text('Solo préstamo', exact=True).count() == 0
        assert page.locator('.book-cover').bounding_box()['width'] <= 100
        page.evaluate("go('home');go('books',{q:'libro'})")
        page.get_by_text('Libro descargable', exact=True).wait_for()
        assert page.evaluate("bookCalls.filter(x=>x.do==='search').length") == 2
        page.locator('.book-actions button').first.click()
        page.get_by_role('button', name='Guardado', exact=True).wait_for()
        page.locator('[data-key="saved"]').click()
        page.get_by_role('heading', name='Libro descargable', exact=True).wait_for()
        page.get_by_role('button', name='Exportar original', exact=True).wait_for()
        assert page.get_by_role('button', name='Abrir', exact=True).count() == 0
        assert page.evaluate('''async () => {
          const books = await BookShelf.all();
          return books.length === 1 && books[0].original_format === 'txt' &&
            await books[0].original.text() === 'Texto completo.';
        }''')
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_youtube_fixed_low_and_silent_book_transport(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate("store.set('youtube.thumbnailQuality','high');go('youtube')")
        assert page.get_by_text('Calidad de miniaturas', exact=False).count() == 0
        page.evaluate('''() => {const original=bridge.call.bind(bridge);window.thumbArgs=[];
          bridge.call=(op,args)=>{if(op==='youtube_thumbs'){thumbArgs.push(args);return Promise.resolve({images:[]});}return original(op,args);};
          document.querySelector('#view').prepend(youtubeThumbnail({url:'https://youtube.com/watch?v=abcdefghijk',title:'Miniatura sintética'}));}''')
        page.wait_for_function('thumbArgs.length > 0')
        assert page.evaluate("thumbArgs.every(args=>args.quality==='low')")
        page.evaluate('''() => {window.__delay=60000; window.coverRid=bridge.request('books',{do:'cover',url:'https://example.org/a.jpg'}).id; activity();}''')
        assert page.evaluate('bridge.pending()[window.coverRid].quiet') is True
        assert page.locator('#activity').is_hidden()
        page.evaluate('bridge.forget(window.coverRid)')
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_model_notice_reconciles_selectable_provider_and_identity(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate('''() => {const original=bridge.call.bind(bridge);bridge.call=(op,args)=>op==='models'?Promise.resolve({now:now(),reset:now()+3600,pin:'',images:[],providers:[
          {key:'fixture',name:'Proveedor nuevo',daily:20,used:0,models:[{key:'fixture:fresh',id:'fresh',title:'Modelo nuevo',daily:20,used:0,roles:['chat']}]}]}):original(op,args);
          go('ai');bridge.pushes.notice({kind:'ok',es:'Catálogo actualizado',en:'Updated catalogue',models_changed:true});}''')
        page.locator('.provider-card > summary').get_by_text('Proveedor nuevo', exact=True).wait_for()
        page.locator('.provider-card > summary').click()
        page.locator('.model-category > summary').click()
        model = page.locator('.model-choice').filter(has_text='Modelo nuevo')
        assert model.locator('.brand-generic .ic').count() == 0
        model.click()
        page.wait_for_function("current.params.space==='fixture:fresh'")
        assert errors == []


def test_library_search_omits_catalogue_only_records(monkeypatch):
    monkeypatch.setattr(book_library, '_cache', {})
    monkeypatch.setattr(book_library, '_search', lambda *a: ([{'id':'catalogue','downloads':[]},{'id':'free','downloads':[{'format':'txt','url':'https://example.org/book.txt'}]}], False))
    assert [row['id'] for row in book_library.search('gutenberg','fixture')['books']] == ['free']


def test_exact_pre_polish_package_is_preserved():
    epoch='test-polish-epoch'
    digest=hashlib.sha256(('epoch:'+epoch).encode())
    manifest=json.loads((app_compat._POLISH_BEFORE/'manifest.json').read_text())
    assert hashlib.sha256((app_compat._POLISH_BEFORE/'live.js').read_bytes()).hexdigest() == manifest['live.js']
    for name in miniapp.APP_FILES:
        if name not in manifest:
            continue  # new assets never belonged to the archived package
        source=app_compat._POLISH_BEFORE if name in app_compat._POLISH_REPLACED else miniapp.APP_DIR
        digest.update(name.encode()+(source/name).read_bytes())
    assert app_compat.polish_previous_build(epoch,str(miniapp.APP_DIR),miniapp.APP_FILES)==digest.hexdigest()[:10]


@pytest.fixture
def group_state(monkeypatch):
    original=copy.deepcopy(state.data)
    monkeypatch.setenv('GROUP_PUBLIC_TOOLS','1')
    monkeypatch.setattr(config,'OWNER_ADDRESS','owner@example.org')
    monkeypatch.setattr(state,'save',lambda:None)
    state.data['public_groups']={}
    rpc=mock.Mock()
    owner=SimpleNamespace(address='owner@example.org',is_key_contact=False,e2ee_avail=True)
    rpc.get_contact.return_value=owner
    group=SimpleNamespace(chat_type='Group',is_encrypted=True,is_contact_request=False,name='Grupo de prueba')
    private=SimpleNamespace(chat_type='Single',is_encrypted=True)
    rpc.get_basic_chat_info.side_effect=lambda account,cid:private if cid==10 else group
    rpc.get_chat_contacts.return_value=[7]
    rpc.send_msg.return_value=123
    monkeypatch.setattr(group_tools,'owner_verified',lambda *a:False)
    monkeypatch.setattr(group_tools.identity_guard,'contact_fingerprint',lambda *a:'A'*40)
    monkeypatch.setattr(group_tools.chat,'rpc',lambda:rpc)
    monkeypatch.setattr(group_tools.chat,'account',lambda:1)
    monkeypatch.setattr(group_tools.chat,'sender',lambda:'owner@example.org')
    yield rpc,group
    state.data.clear();state.data.update(original)


def test_legacy_group_requires_private_approval_and_same_owner_key(group_state,monkeypatch):
    rpc,info=group_state
    message=SimpleNamespace(text='/michiflin activar',chat_id=42,from_id=7,file=None,show_padlock=True)
    assert group_tools.offer(SimpleNamespace(rpc=rpc),1,message,info)
    assert not state.data['public_groups']['42']['enabled']
    result=group_tools.manage(10,{'do':'enable','id':42})
    assert result['groups'][0]['enabled']
    assert group_tools.owner_present(rpc,1,42)
    monkeypatch.setattr(group_tools.identity_guard,'contact_fingerprint',lambda *a:'B'*40)
    assert not group_tools.owner_present(rpc,1,42)


def test_group_private_approval_rejects_foreign_identity_and_group_context(group_state,monkeypatch):
    monkeypatch.setattr(group_tools.chat,'sender',lambda:'stranger@example.org')
    with pytest.raises(ValueError,match='owner_required'):group_tools.manage(10,{'do':'status'})
    monkeypatch.setattr(group_tools.chat,'sender',lambda:'owner@example.org')
    with pytest.raises(ValueError,match='private_encrypted_chat_required'):group_tools.manage(42,{'do':'status'})


def test_group_approval_refuses_expiry_disabled_server_and_missing_membership(group_state,monkeypatch):
    rpc,_=group_state
    state.data['public_groups']['42']={'enabled':False,'requested':0}
    with pytest.raises(ValueError,match='request_expired'):group_tools.manage(10,{'do':'enable','id':42})
    import time
    state.data['public_groups']['42']['requested']=time.time()
    monkeypatch.setenv('GROUP_PUBLIC_TOOLS','0')
    with pytest.raises(ValueError,match='groups_disabled'):group_tools.manage(10,{'do':'enable','id':42})
    monkeypatch.setenv('GROUP_PUBLIC_TOOLS','1')
    rpc.get_chat_contacts.side_effect=lambda account,cid:[7] if cid==10 else [8]
    with pytest.raises(ValueError,match='owner_key_required'):group_tools.manage(10,{'do':'enable','id':42})


def test_model_catalogue_invalidates_all_private_apps_with_only_owner_system_notification(monkeypatch):
    original=copy.deepcopy(state.data)
    state.data['apps']={'101':{'chat':10,'addr':'owner@example.org'},'102':{'chat':11,'addr':'guest@example.org'}}
    monkeypatch.setattr(config,'OWNER_ADDRESS','owner@example.org')
    pushed=mock.Mock()
    monkeypatch.setattr(miniapp,'push',pushed)
    try:
        miniapp._on_health('ok','Modelos actualizados','Updated models')
        assert {call.args[0] for call in pushed.call_args_list}=={10,11}
        assert all(call.args[2]['models_changed'] for call in pushed.call_args_list)
        assert next(call.args[3] for call in pushed.call_args_list if call.args[0]==10)
        assert next(call.args[3] for call in pushed.call_args_list if call.args[0]==11)==''
    finally:
        state.data.clear();state.data.update(original)


def test_settings_place_each_backend_control_in_its_coherent_category():
    with browser_fixture('es', {'width':390,'height':844}) as (page,errors):
        page.evaluate('''() => { cache.settings=[{title:'Mixed legacy group',items:[
          {key:'language',label:'Idioma del chat único',kind:'text',value:'es'},
          {key:'image_size',label:'Imagen IA única',kind:'text',value:'512'},
          {key:'reader_kb',label:'Lector web único',kind:'text',value:'90'},
          {key:'video_height',label:'Video único',kind:'text',value:'360'}]}]; }''')
        for section,label in [('appearance','Idioma del chat único'),('ai','Imagen IA única'),('navigation','Lector web único'),('bot','Video único')]:
            page.evaluate("section=>go('settings',{section})",section)
            assert page.get_by_text(label,exact=True).is_visible()
            assert page.locator('.setting').filter(has=page.locator('input')).count()==1
        assert errors==[]
