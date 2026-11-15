"""Premium themes use only local CSS and a synthetic Delta bridge."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


PREMIUM = {
    "pearl": ("Nácar", "Pearl"), "onyx": ("Ónix", "Onyx"),
    "emerald": ("Esmeralda", "Emerald"), "sapphire": ("Zafiro", "Sapphire"),
    "amethyst": ("Amatista", "Amethyst"), "ruby": ("Rubí", "Ruby"),
    "copper": ("Cobre", "Copper"), "arctic": ("Ártico", "Arctic"),
    "mocha": ("Moca", "Mocha"), "aurora": ("Aurora", "Aurora"),
}


@pytest.mark.parametrize("language,engine,width", [("es", "webkit", 320), ("en", "chromium", 390)])
def test_premium_selector_is_bilingual_accessible_and_persistent(language, engine, width):
    with browser_fixture(language, {"width": width, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("go('settings')")
        style_label = "Estilo visual" if language == "es" else "Visual style"
        accent_label = "Color de acento" if language == "es" else "Accent color"
        styles = page.get_by_label(style_label)
        options = styles.locator("option").evaluate_all("items => items.map(item => [item.value,item.textContent])")
        assert len(options) == len(set(key for key, _ in options)) == 14
        for key, names in PREMIUM.items():
            assert [key, names[language == "en"]] in options
            styles.select_option(key)  # exercise the actual accessible setting
            assert page.evaluate("document.documentElement.dataset.style") == key
            assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--look-banner-a').trim()")
            assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        page.get_by_label(accent_label).select_option("rose")
        page.get_by_role("tab", name="Oscuro" if language == "es" else "Dark", exact=True).click()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("document.documentElement.dataset.style === 'aurora'")
        assert page.evaluate("[document.documentElement.dataset.theme,document.documentElement.dataset.accent]") == ["dark", "rose"]
        page.evaluate("go('settings')")
        assert page.get_by_label(style_label).input_value() == "aurora"
        assert page.get_by_label(accent_label).input_value() == "rose"
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_premium_contrast_accent_fills_banners_and_authentic_brands(engine):
    """Measure key text and controls against conservative tinted backgrounds."""
    with browser_fixture("en", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at > 0")
        rows = page.evaluate("""styles => {
          const noMotion=document.createElement('style');
          noMotion.textContent='*{transition:none!important;animation:none!important}';document.head.appendChild(noMotion);
          const button=document.createElement('button');button.className='btn primary';document.body.appendChild(button);
          const chip=document.createElement('span');chip.className='chip on';document.body.appendChild(chip);
          const brand=brandMark('gemini');document.body.appendChild(brand);
          const original=brand.querySelector('img').getAttribute('src');
          const hero=document.createElement('div');hero.className='hero';document.body.appendChild(hero);
          const rate=document.createElement('div');rate.className='rate-banner';document.body.appendChild(rate);
          const weather=document.createElement('div');weather.className='weather-banner';document.body.appendChild(weather);
          function rgba(color) { const e=document.createElement('span');e.style.color=color;document.body.appendChild(e);
            const v=getComputedStyle(e).color.match(/[\\d.]+/g).map(Number);e.remove();return v; }
          function lum(rgb) {return rgb.slice(0,3).map(n=>{n/=255;return n<=.04045?n/12.92:((n+.055)/1.055)**2.4})
            .reduce((sum,x,i)=>sum+x*[.2126,.7152,.0722][i],0);}
          function contrast(a,b) {const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);}
          function blend(f,b) {const a=f[3]??1;return b.map((v,i)=>i<3?f[i]*a+v*(1-a):1);}
          const explicit={teal:'#086d60',violet:'#5f428f',blue:'#17598f',rose:'#912c56'};
          const rows=[];
          for (const style of styles) for (const theme of ['light','dark'])
            for (const accent of ['style','teal','violet','blue','rose']) {
              prefs.style=style;prefs.theme=theme;prefs.accent=accent;applyLook();
              const root=getComputedStyle(document.documentElement);
              const surface=rgba(root.getPropertyValue('--surface'));
              const surface2=rgba(root.getPropertyValue('--surface-2'));
              const soft=rgba(root.getPropertyValue('--accent-soft'));
              const rail=blend(soft,surface), selected=blend(soft,rail);
              const fill=rgba(root.getPropertyValue('--accent-fill'));
              const nav=getComputedStyle(document.querySelector('.rail .tab.on'));
              const b=getComputedStyle(button), c=getComputedStyle(chip);
              const stops=['--look-banner-a','--look-banner-b','--look-banner-c'].map(k=>rgba(root.getPropertyValue(k)));
              // Model a faint white glaze and 80%-opacity white labels.
              const banner=Math.min(...stops.map(stop=>{const bg=blend([255,255,255,.06],stop);
                return contrast(blend([255,255,255,.8],bg),bg);}));
              const bannerA='rgb('+stops[0].slice(0,3).join(', ')+')';
              rows.push({style,theme,accent, text:contrast(rgba(root.getPropertyValue('--text')),surface),
                muted:Math.min(contrast(rgba(root.getPropertyValue('--muted')),surface2),contrast(rgba(root.getPropertyValue('--muted')),rail)),
                accentText:Math.min(contrast(rgba(root.getPropertyValue('--accent')),surface2),
                  contrast(rgba(root.getPropertyValue('--accent')),blend(soft,surface))),
                nav:contrast(rgba(nav.color),selected), button:contrast(rgba(b.color),rgba(b.backgroundColor)),
                chip:contrast(rgba(c.color),rgba(c.backgroundColor)), banner,
                fillMatches:accent==='style'||fill.slice(0,3).join(',')===rgba(explicit[accent]).slice(0,3).join(','),
                buttonsMatch:rgba(b.backgroundColor).slice(0,3).join(',')===fill.slice(0,3).join(',') &&
                  rgba(c.backgroundColor).slice(0,3).join(',')===fill.slice(0,3).join(','),
                themedBanners:[hero,rate,weather].every(node=>getComputedStyle(node).backgroundImage.includes(bannerA)),
                logo:original.startsWith('data:image/') && brand.querySelector('img').getAttribute('src')===original &&
                  getComputedStyle(brand.querySelector('img')).filter==='none'});
            }
          [button,chip,brand,hero,rate,weather,noMotion].forEach(node=>node.remove());return rows;
        }""", list(PREMIUM))
        assert len(rows) == 10 * 2 * 5
        for row in rows:
            assert min(row[k] for k in ("text", "muted", "accentText", "nav", "button", "chip", "banner")) >= 4.5, row
            assert row["fillMatches"] and row["buttonsMatch"] and row["themedBanners"] and row["logo"], row
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_system_modes_and_explicit_accent_override_all_styles(engine):
    with browser_fixture("en", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        variables = """() => {const c=getComputedStyle(document.documentElement);
          return ['--bg','--surface','--surface-2','--text','--muted','--accent','--accent-strong','--accent-fill']
            .map(k=>c.getPropertyValue(k).trim()).concat(c.colorScheme)}"""
        for scheme in ("light", "dark"):
            page.emulate_media(color_scheme=scheme)
            for style in ("michiflin", "material", "pixel", "ocean", *PREMIUM):
                for accent in ("style", "teal", "violet", "blue", "rose"):
                    page.evaluate("x=>{prefs.style=x[0];prefs.accent=x[1];prefs.theme='system';applyLook()}", [style, accent])
                    system = page.evaluate(variables)
                    page.evaluate("v=>{prefs.theme=v;applyLook()}", scheme)
                    assert system == page.evaluate(variables), (engine, scheme, style, accent, system)
                    if accent != "style":
                        # Explicit colors must override the style's button fill even in dark mode.
                        actual = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent-fill').trim()")
                        assert actual == {"teal": "#086d60", "violet": "#5f428f", "blue": "#17598f", "rose": "#912c56"}[accent]
        assert errors == []


@pytest.mark.parametrize("engine,width,height", [("webkit", 320, 568), ("chromium", 1280, 800)])
def test_premium_looks_apply_across_tabs_without_overflow(engine, width, height):
    with browser_fixture("es", {"width": width, "height": height}, engine=engine, touch=width < 900) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("""() => {const h=weatherHome();h.last={at:now(),data:{place:{label:'Habana sintética'},
          current:{code:63,is_day:1,temp:28,description:'Lluvia de prueba'}}};
          store.set('weatherHome',h);bus.emit('weather_home');}""")
        page.emulate_media(reduced_motion="reduce")
        seen_palettes = set()
        for style in PREMIUM:
            page.evaluate("v=>{prefs.style=v;prefs.theme='dark';prefs.accent='style';applyLook()}", style)
            seen_palettes.add(page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--look-banner-a').trim()"))
            for tab in ("home", "ai", "web", "apps", "downloads", "settings"):
                page.evaluate("v=>goTab(v)", tab)
                assert page.evaluate("document.documentElement.dataset.style") == style
                assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
                assert page.evaluate("""() => {const n=document.querySelector('#tabs');
                  return getComputedStyle(n).display==='none'||n.scrollWidth<=n.clientWidth;}""")
                if tab != "settings" or width >= 900:
                    assert "on" in (page.locator(f'.tab[data-tab="{tab}"]:visible').get_attribute("class") or "")
            page.evaluate("goTab('home')")
            for index in range(3):
                page.locator('.home-banner-dot').nth(index).click()
                assert "gradient" in page.locator('.home-banner-slot > *').evaluate("e=>getComputedStyle(e).backgroundImage")
            # All three banners move normally, respect reduced motion, and use
            # the stored rain observation without an extra weather request.
            before = page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='weather').length")
            page.emulate_media(reduced_motion="no-preference")
            page.locator('.home-banner-dot').nth(0).click()
            assert page.locator('.hero-art .ic:not(.pixel-mascot)').evaluate("e=>getComputedStyle(e).animationName") != "none"
            page.locator('.home-banner-dot').nth(1).click()
            assert page.locator('.rate-banner').evaluate("e=>getComputedStyle(e,'::after').animationName") != "none"
            page.locator('.home-banner-dot').nth(2).click()
            assert page.locator('.weather-banner').get_attribute('data-weather-scene') == 'rain'
            rain = page.locator('.scene-precip-rain')
            assert rain.evaluate("e=>getComputedStyle(e).animationName") != "none"
            page.emulate_media(reduced_motion="reduce")
            assert rain.evaluate("e=>getComputedStyle(e).animationName") == "none"
            page.locator('.home-banner-dot').nth(1).click()
            assert page.locator('.rate-banner').evaluate("e=>getComputedStyle(e,'::after').animationName") == "none"
            page.locator('.home-banner-dot').nth(0).click()
            assert page.locator('.hero-art .ic:not(.pixel-mascot)').evaluate("e=>getComputedStyle(e).animationName") == "none"
            assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='weather').length") == before
        assert len(seen_palettes) == len(PREMIUM)
        assert errors == []
