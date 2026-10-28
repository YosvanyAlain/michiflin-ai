"""Bounded, disposable online preflight; never print provider responses or keys."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from michiflin import ai, coder, state


def main() -> int:
    state.load()
    ai.build_registry()
    print("Coder shell isolated:", coder.isolation(), flush=True)
    if not coder.isolation():
        print("RESULT: isolation unavailable", flush=True)
        return 1
    request = ("Build a small Python asyncio hello program. SEARCH for Python asyncio documentation, "
               "OPEN a result, then write main.py and test it with python3 main.py. "
               "Search query: Python asyncio documentation.")
    discovered: set[str] = set()
    observation = coder._research("SEARCH", "Python asyncio documentation", request, discovered, "en")
    print("Public search returned results:", bool(discovered), flush=True)
    if not discovered or observation.startswith("Research failed"):
        print("RESULT: public search unavailable", flush=True)
        return 1
    if not ai.providers():
        print("RESULT: no configured test model", flush=True)
        return 0
    # Four rounds are sufficient for this small request and cap quota usage.
    coder.MAX_STEPS = 4
    coder.ANSWER_TOKENS = 1800
    original_ask = coder._ask
    rounds = 0
    def trace_ask(messages, model_key, sticky):
        nonlocal rounds
        rounds += 1
        response = original_ask(messages, model_key, sticky)
        kinds = [kind for kind, _arg, _body in coder.parse(response.text)]
        print("Model round:", rounds, "ok:", response.ok, "actions:", ",".join(kinds) or "none", flush=True)
        return response
    coder._ask = trace_ask
    try:
        result = coder.run(request, [], "en")
    except Exception as exc:
        # Some provider errors may contain private data; reveal only the class.
        print("RESULT: model/coder error", type(exc).__name__, flush=True)
        return 2
    steps = result.get("steps", [])
    files = result.get("files", [])
    researched = any(item.get("kind") == "research" for item in steps)
    executed = any(item.get("kind") == "run" and item.get("code") == 0 for item in steps)
    built = any(item.get("path") == "main.py" for item in files)
    print("Real model produced main.py:", built, flush=True)
    print("Real model requested research:", researched, flush=True)
    print("Real model ran a successful command:", executed, flush=True)
    # This prompt and all pages read in this test are public; never print secrets
    # or raw provider errors. The bounded summary helps diagnose a no-file reply.
    if not built:
        summary = str(result.get("summary") or "").replace("\n", " ")[:250]
        print("Public test summary:", summary, flush=True)
    print("RESULT:", "full-pass" if built and researched and executed else "partial", flush=True)
    return 0 if built and researched and executed else 3


if __name__ == "__main__":
    raise SystemExit(main())
