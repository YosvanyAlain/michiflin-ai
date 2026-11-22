"""One-shot development activation must not become recurring inference hosting."""
import copy
import json
from pathlib import Path
from unittest import mock

import pytest

from tools import smoke_local_hybrid as smoke


@pytest.fixture
def paths(monkeypatch, tmp_path):
    root = tmp_path / 'michiflin-synthetic-preflight'
    values = {'RUNNER_TEMP': str(tmp_path), 'MICHIFLIN_SYNTHETIC_PREFLIGHT_DIR': str(root),
              'CONFIG_DIR': str(root / 'config'), 'JOB_DIR': str(root / 'jobs'),
              'PREVIEW_DIR': str(root / 'preview'), 'LOCAL_HYBRID_REPORT': str(tmp_path / 'local-hybrid-preflight.json')}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_synthetic_preflight_uses_only_its_separate_directories(paths):
    assert smoke.validate_paths() == Path(paths['LOCAL_HYBRID_REPORT'])
    assert Path(paths['CONFIG_DIR']).is_dir()


@pytest.mark.parametrize('key', ['CONFIG_DIR', 'JOB_DIR', 'PREVIEW_DIR', 'LOCAL_HYBRID_REPORT', 'MICHIFLIN_SYNTHETIC_PREFLIGHT_DIR'])
def test_synthetic_preflight_refuses_live_or_arbitrary_paths(paths, monkeypatch, key, tmp_path):
    monkeypatch.setenv(key, str(tmp_path / 'live-state'))
    with pytest.raises(ValueError):
        smoke.validate_paths()


def test_one_shot_workflow_is_opt_in_bounded_and_does_not_forward_activation():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/michiflin.yml').read_text()
    assert 'local_ai_test:' in workflow and 'default: false' in workflow
    assert "vars.LOCAL_AI_TEST_RUN_NUMBER == github.run_number" in workflow
    assert "github.run_attempt == '1'" in workflow
    assert "vars.LOCAL_GGUF_TEST_REQUESTED || '0'" in workflow
    assert 'echo "SESSION_DURATION_SECONDS=3600"' in workflow
    assert 'cancel-in-progress: false' in workflow
    assert 'CONFIG_DIR: ${{ runner.temp }}/michiflin-synthetic-preflight/config' in workflow
    next_session = workflow[workflow.index('      - name: Next session'):]
    assert 'local_ai_test' not in next_session and 'LOCAL_AI_TEST_RUN_NUMBER' not in next_session
    failure = workflow[workflow.index('      - name: Keep normal bot if hybrid preflight failed'):]
    assert 'echo "LOCAL_GGUF_ENABLED=0"' in failure
    assert 'LOCAL_WEB_SYNTHESIS=0' in failure and 'LOCAL_AGENT_COORDINATOR=0' in failure


def test_smoke_report_exercises_real_quota_settlement_with_fake_local_transport(paths, monkeypatch):
    from michiflin import admin, local_ai, smart_agent, state
    saved = copy.deepcopy(state.data)
    try:
        state.data['people'] = {}
        state.data['settings'] = {}
        monkeypatch.setattr(state, '_save_soon', lambda: None)
        monkeypatch.setattr(admin, 'is_admin', lambda address: False)
        monkeypatch.setattr(state, 'check_work_allowed', lambda: None)
        monkeypatch.setattr(local_ai, 'configured', lambda: True)
        monkeypatch.setattr(local_ai, 'ready', lambda: True)
        monkeypatch.setattr(local_ai, '_flag', lambda name: True)
        monkeypatch.setattr(local_ai, 'complete', lambda *a, **kw: local_ai.Result('ok', 'Una copia guarda tus datos.'))
        source = local_ai.Source('S1', 'Synthetic', 'https://example.test', 'Horario no confirmado.')
        monkeypatch.setattr(local_ai, '_extractive', lambda *a, **kw: local_ai.Result('ok', 'Horario no confirmado [S1]', (source,), method='web'))
        monkeypatch.setattr(smart_agent, '_local_action', lambda *a, **kw: {'tool': 'calculator', 'input': '3*9-5'})
        with mock.patch('requests.sessions.Session.request', side_effect=AssertionError('external request')):
            smoke.main()
        report = json.loads(Path(paths['LOCAL_HYBRID_REPORT']).read_text())
        assert report['status'] == 'ok' and report['external_provider_calls'] == 0
        assert report['hybrid']['api_transport_calls'] == 1
        assert report['hybrid']['per_person_quota_enforced']
        assert all(row['answer'] == '22' for row in report['hybrid']['calculator_calls'])
    finally:
        state.data.clear()
        state.data.update(saved)
        state.act_as('')


def test_extended_app_input_reaches_agent_intact_and_refuses_overflow(monkeypatch):
    from michiflin import agent, collab_agent, miniapp
    monkeypatch.setenv('LOCAL_GGUF_EXTENDED', '1')
    monkeypatch.setattr(collab_agent, 'authorized', lambda scope: False)
    monkeypatch.setattr(miniapp, 'run_captured', lambda callback, locale: callback() or {'text': 'fixture'})
    text = 'Explica este texto. ' * 450
    with mock.patch.object(agent, 'handle', return_value=None) as handle:
        miniapp.op_ask(1, {'space': 'agent', 'text': text}, 'es')
        assert handle.call_args.args[1] == text.strip()
        with pytest.raises(miniapp.AppError):
            miniapp.op_ask(1, {'space': 'agent', 'text': 'x' * 12001}, 'es')
    monkeypatch.setenv('LOCAL_GGUF_EXTENDED', '0')
    with pytest.raises(miniapp.AppError):
        miniapp.op_ask(1, {'space': 'agent', 'text': text}, 'es')


@pytest.mark.parametrize('available', [False, True])
def test_real_extractor_ranks_the_exact_preflight_fixture_before_synthesis(monkeypatch, available):
    from michiflin import local_ai
    monkeypatch.setenv('LOCAL_WEB_SYNTHESIS', '1')
    monkeypatch.setattr(local_ai, 'configured', lambda: True)
    sources = (local_ai.Source('S1', 'Biblioteca sintética', 'https://example.test/library', smoke.PUBLIC_EXCERPT),)
    raw = local_ai.Result('ok', '{"answer":"El horario del sábado no está confirmado [S1]."}') if available else local_ai.Result('unavailable')
    with mock.patch.object(local_ai, 'complete_messages', return_value=raw) as infer:
        result = local_ai._extractive(smoke.PUBLIC_QUESTION, sources, 'es', synthesize=True)
    assert result.status == 'ok' and result.sources == sources
    assert 'no está confirmado' in result.text and '[S1]' in result.text
    assert infer.called
    assert ('Síntesis de IA local' in result.text) == available


def test_development_runs_have_fixed_duration_choices_and_do_not_multiply_model_downloads():
    root = Path(__file__).resolve().parents[1]
    runtime = (root / '.github/workflows/michiflin.yml').read_text()
    compare = (root / '.github/workflows/local-model-compare.yml').read_text()
    assert "options: ['1', '2', '3']" in runtime
    assert 'case "$LOCAL_AI_TEST_HOURS" in' in runtime
    assert 'SESSION_DURATION_SECONDS=10800' in runtime
    assert 'SESSION_DURATION_SECONDS=7200' in runtime
    assert 'SESSION_DURATION_SECONDS=3600' in runtime
    assert 'include_legacy:' in compare and 'max-parallel: 1' in compare
    assert "|| '[\"qwen35\"]'" in compare
    assert 'cache: pip' in compare and 'actions/cache' not in compare
    assert 'smoke_local_hybrid.py' in compare
    assert 'playwright install' not in compare
