"""User-accepted agent behavior must not drift while pending work is added.

Updating this manifest is an explicit new-version decision, not an automatic
side effect of provider discovery or workflow changes. Hashes are regression
checks, not a security signature or a guarantee of response determinism.
"""
import ast
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / 'docs/stable-agents-profile.json').read_text())


@pytest.mark.parametrize('path,expected', list(PROFILE['files'].items()))
def test_accepted_agent_files_are_unchanged(path, expected):
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, (
        f'{path} changed from the user-accepted baseline; review and version the change explicitly')


@pytest.mark.parametrize('path,name,expected', [
    (path, name, digest) for path, functions in PROFILE['functions'].items() for name, digest in functions.items()
])
def test_accepted_api_quota_and_routing_functions_are_unchanged(path, name, expected):
    tree = ast.parse((ROOT / path).read_text())
    function = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    actual = hashlib.sha256(ast.dump(function, include_attributes=False).encode()).hexdigest()
    assert actual == expected, f'{path}:{name} changed; preserve the accepted behavior or explicitly version it'


def test_accepted_profile_remains_identifiable_and_reversible():
    assert PROFILE['source_revision'] == '5da5d2bb4e42f9bce3f6c37ac5f24a92860c4da0'
    assert PROFILE['name'] == 'agentes-estables-20261119'
    assert PROFILE['profile']['context_tokens'] == 6144
    assert PROFILE['profile']['LOCAL_GGUF_VARIANT'] == 'qwen35'
    assert PROFILE['profile']['LOCAL_AGENT_COORDINATOR'] == '1'
