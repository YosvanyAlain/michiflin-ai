"""Real GGUF, actual hybrid code, synthetic inputs and a fake API transport only.

Must run with separate temporary config/jobs/preview directories. Never loads a
Delta Chat account or saved state, never contacts a provider/search website, and
never reads a private prompt. The server is the authenticated isolated GGUF.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from unittest import mock


PUBLIC_EXCERPT = (
    'La biblioteca abre de lunes a viernes de 09:00 a 17:00 para atender a todas las personas que visitan sus salas de lectura. '
    'El horario del sábado de la biblioteca no está confirmado, por lo que este extracto no permite afirmar que abra ese día.'
)
PUBLIC_QUESTION = '¿Está confirmado el horario del sábado?'


def validate_paths() -> Path:
    root = Path(os.environ['MICHIFLIN_SYNTHETIC_PREFLIGHT_DIR']).resolve()
    runner = Path(os.environ['RUNNER_TEMP']).resolve()
    if root != runner / 'michiflin-synthetic-preflight':
        raise ValueError('synthetic preflight root refused')
    for key, sub in [('CONFIG_DIR', 'config'), ('JOB_DIR', 'jobs'), ('PREVIEW_DIR', 'preview')]:
        path = Path(os.environ[key]).resolve()
        if path != root / sub:
            raise ValueError('synthetic preflight directory refused')
        path.mkdir(parents=True, exist_ok=True)
    output = Path(os.environ['LOCAL_HYBRID_REPORT']).resolve()
    if output != runner / 'local-hybrid-preflight.json':
        raise ValueError('synthetic report location refused')
    return output


def main():
    output = validate_paths()  # validate before importing any application module
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from michiflin import ai, local_ai, smart_agent, state, tools, usage
    started = time.monotonic()
    report = {'synthetic_only': True, 'bot_account_started': False,
              'real_local_inference': True, 'external_api_transport': 'mocked',
              'external_provider_calls': 0, 'status': 'failed'}
    try:
        assert local_ai.configured() and local_ai.ready(), 'local server unavailable'
        assert all(local_ai._flag(name) for name in (
            'LOCAL_GGUF_EXTENDED', 'LOCAL_WEB_SYNTHESIS', 'LOCAL_AGENT_COORDINATOR'))
        # Accidental HTTP requests fail before leaving the process. GGUF uses a
        # local authenticated Unix socket through http.client, not requests.
        with mock.patch('requests.sessions.Session.request', side_effect=AssertionError('external HTTP forbidden')):
            question = 'Responde brevemente en español: ¿qué es una copia de seguridad?'
            result = local_ai.complete(question, 'es', sender='synthetic-local@example.test')
            report['chat'] = {'status': result.status, 'text': result.text, 'truncated': result.truncated}
            assert result.status == 'ok' and result.text and not result.truncated
            assert usage.used_today('synthetic-local@example.test', 'ask') == 0
            sources = (local_ai.Source('S1', 'Biblioteca sintética', 'https://example.test/library',
                       PUBLIC_EXCERPT),)
            synthesis = local_ai._extractive(PUBLIC_QUESTION, sources, 'es',
                         sender='synthetic-local@example.test', synthesize=True)
            report['web'] = {'status': synthesis.status, 'text': synthesis.text,
                             'synthesis_accepted': 'Síntesis de IA local' in synthesis.text,
                             'sources': [s.label for s in synthesis.sources]}
            assert synthesis.status == 'ok' and synthesis.sources
            # The actual quota wrapper and the real calculator remain active.
            # Only the provider transport is replaced. Inspect and retain the
            # proposal rather than pretending a local fallback used a tool.
            state.act_as('synthetic-hybrid@example.test')
            state.data['settings']['ai_daily'] = 1
            state.data['settings']['ai_reserve'] = 0
            original_plan = smart_agent._local_action
            proposals = []
            calculations = []
            original_calculate = tools.calculate
            def plan(*args, **kwargs):
                value = original_plan(*args, **kwargs)
                proposals.append(value)
                return value
            def calculate(expression):
                answer = original_calculate(expression)
                calculations.append({'expression': expression, 'answer': answer})
                return answer
            def transport(messages, *_args, **_kwargs):
                # Synthetic collaborator, not evidence of a real provider call.
                return ai.AIResult(text=json.dumps({'answer': 'Respuesta de API simulada; revisión de observaciones sintéticas.'}),
                                   provider='Synthetic API transport', model='fixture')
            with mock.patch.object(smart_agent, '_local_action', side_effect=plan), \
                 mock.patch.object(tools, 'calculate', side_effect=calculate), \
                 mock.patch.object(tools, 'search', side_effect=AssertionError('external search forbidden')), \
                 mock.patch.object(tools, 'weather_text', side_effect=AssertionError('external weather forbidden')), \
                 mock.patch.object(smart_agent, '_read', side_effect=AssertionError('external page forbidden')), \
                 mock.patch.object(ai, '_complete', side_effect=transport) as api_transport:
                hybrid = smart_agent.run('Calcula 3*9-5 con la calculadora y explica el resultado.', 'es')
                assert api_transport.call_count == 1, 'unexpected API transport count'
                assert usage.used_today('synthetic-hybrid@example.test', 'ask') == 1
                state.act_as('synthetic-hybrid@example.test')
                try:
                    smart_agent.run('Calcula 2+2', 'es')
                except smart_agent.AgentError:
                    pass
                else:
                    raise AssertionError('exhausted per-person quota was bypassed')
                assert api_transport.call_count == 1
                assert usage.used_today('synthetic-local@example.test', 'ask') == 0
            report['hybrid'] = {'local_proposals': proposals, 'calculator_calls': calculations,
                               'api_transport_calls': api_transport.call_count,
                               'signature': hybrid['sig'], 'per_person_quota_enforced': True}
            report['status'] = 'ok'
    except Exception as exc:
        report['failure_type'] = type(exc).__name__  # never publish arbitrary exception bodies
        raise
    finally:
        state.act_as('')
        report['seconds'] = round(time.monotonic() - started, 2)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('SAFE_GGUF_HYBRID_PREFLIGHT=' + report['status'])


if __name__ == '__main__':
    main()
