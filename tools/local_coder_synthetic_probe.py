"""Isolated synthetic-only integration probe: no bot state or account."""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from michiflin import ai, chat, coder, local_ai, local_coder


def verify_files(files, command, expected):
    folder = Path(tempfile.mkdtemp(prefix="coder-probe-"))
    folder.chmod(0o777)
    try:
        assert coder.write_files(folder, files)
        code, out = coder._run(folder, command)
        print("BEHAVIOR", json.dumps({"command": command, "exit": code, "output": out[:300]}), flush=True)
        assert code == 0 and out.strip() == expected
    finally:
        coder._remove(folder)


def exercise(label, request, files, target, verify_command, expected):
    start = time.monotonic()
    result = coder.run(request, files, "es", local_coder.MODEL_KEY)
    print("PROJECT", json.dumps({"case": label, "seconds": round(time.monotonic() - start, 1),
                                 "complete": result["project_complete"], "confirmed": result["model_confirmed"],
                                 "isolated": result["isolated"], "summary": result["summary"],
                                 "files": [item["path"] for item in result["files"]], "steps": result["steps"]}, ensure_ascii=False), flush=True)
    assert result["isolated"] and result["project_complete"] and result["model_confirmed"]
    assert any(item["path"] == target for item in result["files"])
    assert any(step["kind"] == "run" and step.get("cmd", "").startswith(("python3 ", "gcc ")) and step.get("code") == 0 for step in result["steps"])
    verify_files(result["files"], verify_command, expected)
    return result["files"]


def main():
    assert local_ai.ready(), "local model not ready"
    assert coder.isolation(), "isolated compiler/runner unavailable"
    real_complete = local_ai.complete_messages
    original_http = local_ai._http
    inference_started = threading.Event()
    queue_seen = threading.Event()
    starts = []
    lock = threading.Lock()

    def observed_http(path, payload, timeout):
        if path == "/v1/chat/completions":
            last = payload["messages"][-1]["content"]
            who = "chat" if last == "Respond with exactly OK." else "coder"
            with lock:
                starts.append((who, time.monotonic()))
            if who == "coder":
                inference_started.set()
        return original_http(path, payload, timeout)

    def observed_complete(messages, **kwargs):
        before = time.monotonic()
        result = real_complete(messages, **kwargs)
        print("MODEL", json.dumps({"coding": bool(kwargs.get("coding")), "status": result.status,
                                   "truncated": result.truncated, "seconds": round(time.monotonic() - before, 1),
                                   "synthetic_output": result.text[:2400]}, ensure_ascii=False), flush=True)
        return result

    with mock.patch.object(ai, "complete", side_effect=AssertionError("external provider used")), \
         mock.patch.object(ai, "complete_exact", side_effect=AssertionError("external provider used")), \
         mock.patch.object(chat, "sender", return_value="synthetic-coder@example.test"), \
         mock.patch.object(local_ai, "_http", side_effect=observed_http), \
         mock.patch.object(local_ai, "complete_messages", side_effect=observed_complete):
        files = exercise("python", "Crea saludo.py, un programa que imprima exactamente HOLA y termine. "
                         "Incluye un bloque RUN para probarlo con python3 y termina con DONE.",
                         [{"path": "conservar.txt", "text": "original"}], "saludo.py", "python3 saludo.py", "HOLA")
        assert next(x for x in files if x["path"] == "conservar.txt")["text"] == "original"
        files = exercise("edit", "Cambia saludo.py para que imprima exactamente ADIOS. Conserva los demás archivos. "
                         "Incluye RUN para probar con python3 y termina con DONE.", files,
                         "saludo.py", "python3 saludo.py", "ADIOS")
        assert next(x for x in files if x["path"] == "conservar.txt")["text"] == "original"
        exercise("c-compile", "Crea main.c: un programa C completo que imprima exactamente OK. "
                 "Incluye un bloque RUN para compilar con gcc y ejecutar, y termina con DONE.", [],
                 "main.c", "gcc main.c -o test-c && ./test-c", "OK")

        # A local coding round and a regular agent turn share the same FIFO.
        inference_started.clear(); starts.clear()
        outcomes = {}
        def code_task():
            try:
                outcomes["code"] = coder.run("Crea tiny.py que imprima 7, incluye RUN python3 tiny.py y DONE.", [], "es", local_coder.MODEL_KEY)
            except Exception as exc:
                outcomes["code_error"] = str(exc)
        def chat_task():
            outcomes["chat"] = local_ai.complete("Respond with exactly OK.", "en", sender="synthetic-chat@example.test", on_wait=queue_seen.set)
        t1 = threading.Thread(target=code_task); t1.start(); assert inference_started.wait(30)
        t2 = threading.Thread(target=chat_task); t2.start(); assert queue_seen.wait(20)
        t1.join(260); t2.join(260)
        assert not t1.is_alive() and not t2.is_alive()
        assert outcomes.get("chat") and outcomes["chat"].status == "ok"
        assert starts[0][0] == "coder" and starts[1][0] == "chat", starts
        print("FIFO", json.dumps({"order": [x[0] for x in starts],
                                  "offset_seconds": [round(x[1] - starts[0][1], 1) for x in starts],
                                  "chat": outcomes["chat"].status,
                                  "coder_confirmed": outcomes.get("code", {}).get("model_confirmed", False)}, ensure_ascii=False), flush=True)
    print("SYNTHETIC_LOCAL_CODER_OK", flush=True)


if __name__ == "__main__":
    main()
