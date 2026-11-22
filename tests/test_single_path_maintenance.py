"""Authorized maintenance: one main route, no duplicate bots, narrow agent fixes."""
import json
from pathlib import Path
from unittest import mock

import pytest

from michiflin import ai, local_ai, smart_agent, state, tools, usage

ROOT = Path(__file__).resolve().parents[1]
QUOTE = 'El horario del sábado de la biblioteca no está confirmado, por lo que este extracto no permite afirmar que abra ese día.'


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv('LOCAL_AGENT_COORDINATOR', '1')
    monkeypatch.setenv('LOCAL_WEB_SYNTHESIS', '1')
    monkeypatch.setattr(local_ai, 'configured', lambda: True)
    monkeypatch.setattr(state, 'check_work_allowed', lambda: None)
    monkeypatch.setattr(usage, 'allowed', lambda *a: True)
    state.act_as('synthetic@example.test')
    yield
    state.act_as('')


def test_repeated_local_calculator_delegates_with_real_observation(context):
    proposal = {'tool': 'calculator', 'input': '3*9-5'}
    with mock.patch.object(smart_agent, '_local_action', return_value=proposal) as plan, \
         mock.patch.object(tools, 'calculate', return_value='22') as calculate, \
         mock.patch.object(ai, 'complete', return_value=ai.AIResult(text='{"answer":"22"}')) as external:
        smart_agent.run('Calcula 3*9-5', 'es')
    assert plan.call_count == 2
    calculate.assert_called_once_with('3*9-5')
    external.assert_called_once()
    assert '22' in str(external.call_args) and 'UNTRUSTED TOOL DATA' in str(external.call_args)


def test_api_duplicates_reuse_only_this_request_and_exact_argument(context):
    def result(value):
        return ai.AIResult(text=json.dumps(value))
    sequence = [result({'tool': 'calculator', 'input': expression}) for expression in ['2+2', '2+2', '2+3']]
    sequence.append(result({'answer': '4 y 5'}))
    with mock.patch.object(smart_agent, '_local_action', return_value=None), \
         mock.patch.object(tools, 'calculate', side_effect=['4', '5', '4']) as calculate, \
         mock.patch.object(ai, 'complete', side_effect=sequence):
        smart_agent.run('Calcula 2+2 y 2+3', 'es')
    assert [call.args[0] for call in calculate.call_args_list] == ['2+2', '2+3']
    with mock.patch.object(smart_agent, '_local_action', return_value=None), \
         mock.patch.object(tools, 'calculate', return_value='4') as calculate, \
         mock.patch.object(ai, 'complete', side_effect=[result({'tool': 'calculator', 'input': '2+2'}), result({'answer': '4'})]):
        smart_agent.run('Calcula 2+2', 'es')
    calculate.assert_called_once_with('2+2')  # no cross-request cache


@pytest.mark.parametrize('change,accepted', [
    ({}, True),
    ({'citations': []}, False),
    ({'citations': 'S1'}, False),
    ({'citations': [{'source': 'S9', 'text': QUOTE}]}, False),
    ({'citations': [{'source': 'S1', 'text': 'Invented quotation'}]}, False),
    ({'citations': [{'source': 'S1', 'text': QUOTE, 'url': 'https://evil.test'}]}, False),
    ({'citations': [{'source': [], 'text': QUOTE}]}, False),
    ({'answer': 'Dato https://evil.test'}, False),
    ({'answer': 'Dato [S9]'}, False),
    ({'answer': 'Primero.\n\nSegundo.'}, False),
    ({'answer': 'Dato', 'tool': 'shell'}, False),
])
def test_replay_observed_separate_citations_and_reject_unsafe_variants(context, change, accepted):
    value = {'answer': 'No está confirmado el horario del sábado.',
             'citations': [{'source': 'S1', 'text': QUOTE}]}
    value.update(change)
    sources = (local_ai.Source('S1', 'Biblioteca sintética', 'https://example.test/library', QUOTE),)
    with mock.patch.object(local_ai, 'complete_messages', return_value=local_ai.Result('ok', json.dumps(value))):
        got = local_ai._synthesize_sources('Horario del sábado', [(QUOTE, 0)], sources, 'es')
    assert (got is not None) == accepted
    if accepted:
        assert '[S1]' in got.text and got.sources == sources
        assert got.sources[0].url == 'https://example.test/library'


def test_only_two_workflows_with_explicit_non_bot_tools():
    assert {p.name for p in (ROOT / '.github/workflows').glob('*.yml')} == {'michiflin.yml', 'tests.yml'}
    text = (ROOT / '.github/workflows/tests.yml').read_text()
    assert 'branches: [main]' in text
    assert 'options: [all, tests, model, diagnostic, maintenance]' in text
    assert "inputs.task == 'model'" in text
    assert "inputs.task == 'diagnostic'" in text
    assert 'NEXT_RUNTIME_CONFIG' not in text
    assert 'python bot.py' not in text
    runtime = (ROOT / '.github/workflows/michiflin.yml').read_text()
    assert 'group: michiflin-session' in runtime and 'cancel-in-progress: false' in runtime
    assert 'Final encrypted backup failed; automatic handover is stopped.' in runtime
