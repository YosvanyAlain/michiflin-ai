"""Synthetic UI regression: bounded restart estimates and narrow usage cards."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine,width', [('chromium', 280), ('chromium', 320), ('webkit', 390), ('chromium', 1280)])
def test_administration_addresses_badges_and_bars_wrap(engine, width):
    with browser_fixture('es', {'width': width, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate('''()=>{
          const address='very.long.synthetic.owner.name.'+'x'.repeat(180)+'@example.invalid';
          const people=[{address,heavy:true,over:true,admin:false,share:0.95,bytes:123456,requests:91,ai:40,images:5,code:8,jobs:3,today:4567}];
          document.documentElement.style.fontSize='24px';
          fill(document.querySelector('#view'),usageSection(document.querySelector('#view'),
            {people,limited:[address],days:30,max_jobs:2,budget_mb:0,paused:false},false,false));
        }''')
        assert page.locator('.share').count() == 1
        assert page.locator('.share .badge').count() == 3
        for selector in ['#view', '.share', '.share > .row', '.share .bar']:
            assert page.locator(selector).evaluate('el=>el.scrollWidth <= el.clientWidth + 1'), selector
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert page.locator('.share b').inner_text().endswith('@example.invalid')
        assert page.locator('.share b').evaluate('el=>el.getBoundingClientRect().height > 20')
        assert errors == []


def test_restart_countdown_uses_bounded_fresh_snapshot_and_reports_unknown():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        result = page.evaluate('''()=>{
          healthObservation=null;const missing=restartLabel();
          const c=healthClock();
          // Server's clock may be different from the device; only its interval matters.
          healthObservation={server:1000000000,restart:1000003600,
            start:{mono:c.mono-2000,wall:c.wall-2000},end:c};
          const normal=restartLabel(), mismatch=restartLabel(999);
          const live=restartCountdown();document.querySelector('#view').append(live);
          return {missing,normal,mismatch};
        }''')
        assert result['missing'] == 'desconocido' and result['mismatch'] == 'desconocido'
        assert 'aprox.' in result['normal'] and result['normal'].count(':') >= 2
        locator = page.locator('#view [data-restart-countdown]').last
        first = locator.inner_text()
        page.wait_for_function("first=>document.querySelector('#view [data-restart-countdown]:last-child').textContent !== first", arg=first, timeout=5000)
        outcomes = page.evaluate('''()=>{
          const c=healthClock();
          healthObservation={server:100,restart:3700,start:{mono:c.mono-20000,wall:c.wall-20000},end:c};
          const delayed=restartLabel();
          healthObservation.end.mono-=121000;healthObservation.end.wall-=121000;
          const stale=restartLabel();
          healthObservation={server:100,restart:3700,start:c,end:{mono:c.mono,wall:c.wall-60000}};
          const clockJump=restartLabel();
          healthObservation={server:100,restart:101,start:{mono:c.mono-5000,wall:c.wall-5000},end:c};
          const ended=restartLabel();
          return {delayed,stale,clockJump,ended};
        }''')
        assert '–' in outcomes['delayed']  # latency shown as a range, not false precision
        assert outcomes['stale'] == outcomes['clockJump'] == outcomes['ended'] == 'desconocido'
        assert errors == []
