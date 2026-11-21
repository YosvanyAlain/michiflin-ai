"""Synthetic real-inference comparison; no bot identity, user data or providers.

Scores are narrow checks, not an intelligence benchmark. Answers are retained
for review and timing; never run generated code on the host.
"""
import http.client
import json
import os
from pathlib import Path
import re
import socket
import time


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/run/michiflin-local-gguf/llama.sock')


def request(payload):
    key = Path(os.environ['RUNNER_TEMP'], 'michiflin-local-gguf-key').read_text().strip()
    connection = UnixConnection('localhost', timeout=125)
    try:
        connection.request('POST', '/v1/chat/completions', body=json.dumps(payload),
                           headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        response = connection.getresponse()
        assert response.status == 200, 'inference HTTP ' + str(response.status)
        return json.loads(response.read(1_000_000))
    finally:
        connection.close()


def main():
    cases = [
        ('arithmetic', 'Una tienda vende 3 cuadernos de 7 euros y devuelve 4 euros de descuento. ¿Cuánto se paga? Responde solo con el número.', lambda s: s.strip() == '17'),
        ('logic', 'Ana es mayor que Bea. Bea es mayor que Carla. ¿Quién es la menor? Responde solo con su nombre.', lambda s: s.strip().rstrip('.') == 'Carla'),
        ('json', 'Devuelve únicamente JSON válido con las claves ciudad y pais, para La Habana y Cuba. No uses bloques Markdown.', lambda s: json.loads(s) == {'ciudad': 'La Habana', 'pais': 'Cuba'}),
        ('uncertainty', '¿Cuál es el precio exacto del dólar hoy en una casa de cambio que no he identificado? No tienes acceso web ni cotización suministrada.', lambda s: bool(re.search(r'no (?:puedo|tengo|dispongo)|necesit|desconozco|no es posible', s, re.I))),
        ('code', 'Escribe en Python una función divide(a,b) que devuelva None si b es cero y a/b en otro caso. No ejecutes código.', lambda s: all(t in s for t in ('def divide', 'None', '/'))),
        ('instruction', 'Ordena alfabéticamente estas palabras y responde una por línea sin explicación: pera, manzana, banana.', lambda s: [v.strip(' -*').strip() for v in s.splitlines() if v.strip()] == ['banana', 'manzana', 'pera']),
        ('reasoning', 'Tengo 5 cajas con 12 lápices cada una. Reparto 18 lápices y después compro 7 más. Explica brevemente cuántos quedan.', lambda s: bool(re.search(r'\b49\b', s)) and not bool(re.search(r'\b(?:47|48|50)\s+lápices\b', s))),
        ('spanish', 'Explícame en español, con un ejemplo breve, la diferencia entre una copia de seguridad y sincronización. No afirmes haber realizado ninguna.', lambda s: len(s) > 120 and 'sincron' in s.lower() and ('copia' in s.lower() or 'respaldo' in s.lower())),
    ]
    rows = []
    for name, prompt, check in cases:
        started = time.monotonic()
        body = request({'messages': [{'role': 'system', 'content': 'Eres un asistente útil y preciso. Responde en español. No tienes herramientas. /no_think'}, {'role': 'user', 'content': prompt}],
                        'temperature': 0.2, 'max_tokens': 512, 'stream': False, 't_max_predict_ms': 90000,
                        'chat_template_kwargs': {'enable_thinking': False}})
        choice = body['choices'][0]
        text = choice['message'].get('content') or ''
        text = re.sub(r'(?s)^\s*<think>.*?</think>\s*', '', text).strip()
        try:
            passed = bool(check(text))
        except (ValueError, TypeError):
            passed = False
        rows.append({'case': name, 'prompt': prompt, 'text': text, 'check_passed': passed,
                     'seconds': round(time.monotonic() - started, 2), 'finish_reason': choice.get('finish_reason'),
                     'usage': body.get('usage', {})})
        print(name, 'check=' + str(passed), 'seconds=' + str(rows[-1]['seconds']), flush=True)
    output = {'variant': os.environ['LOCAL_GGUF_VARIANT'], 'real_inference': True,
              'synthetic_prompts_only': True, 'bot_account_started': False, 'external_provider_calls': 0,
              'narrow_checks_passed': sum(r['check_passed'] for r in rows), 'cases': rows}
    Path('local-benchmark.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    assert all(r['text'] for r in rows), 'empty inference output'


if __name__ == '__main__':
    main()
