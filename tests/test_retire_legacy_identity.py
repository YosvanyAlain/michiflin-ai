"""Private-only retirement must fail closed and never touch a new identity."""
from __future__ import annotations
import pytest
from tools import retire_legacy_identity_once as retirement

ANCHOR = 'a' * 40
HEAD = 'b' * 40
PARENT = 'c' * 40
HEADS = [{'ref': 'refs/heads/main', 'object': {'sha': HEAD}}]
TAGS = [
    {'ref': 'refs/tags/michiflin-fresh-identity-v1', 'object': {'type': 'commit', 'sha': ANCHOR}},
    {'ref': 'refs/tags/michiflin-state-releases-v1', 'object': {'type': 'commit', 'sha': ANCHOR}},
]
COMMITS = [{'sha': HEAD, 'parents': [{'sha': PARENT}]}, {'sha': PARENT, 'parents': []}]
TREES = {HEAD: ['michiflin-state.bin'], PARENT: ['michiflin-state.bin'], ANCHOR: ['.michiflin-backup-anchor']}


def test_git_topology_must_only_contain_old_backup_history_and_safe_anchor():
    assert retirement.validate_git_topology(HEADS, TAGS, COMMITS, TREES, ANCHOR, 'michiflin-state.bin') == HEAD
    extra_branch = HEADS + [{'ref': 'refs/heads/other', 'object': {'sha': HEAD}}]
    with pytest.raises(retirement.RetirementError, match='branches'):
        retirement.validate_git_topology(extra_branch, TAGS, COMMITS, TREES, ANCHOR, 'michiflin-state.bin')
    old_tag = TAGS + [{'ref': 'refs/tags/keep', 'object': {'type': 'commit', 'sha': HEAD}}]
    with pytest.raises(retirement.RetirementError, match='tag'):
        retirement.validate_git_topology(HEADS, old_tag, COMMITS, TREES, ANCHOR, 'michiflin-state.bin')
    modified = {**TREES, PARENT: ['other-user-file', 'michiflin-state.bin']}
    with pytest.raises(retirement.RetirementError, match='unrelated'):
        retirement.validate_git_topology(HEADS, TAGS, COMMITS, modified, ANCHOR, 'michiflin-state.bin')


def test_anchored_main_can_be_checked_after_an_interrupted_cleanup():
    heads = [{'ref': 'refs/heads/main', 'object': {'sha': ANCHOR}}]
    commits = [{'sha': ANCHOR, 'parents': []}]
    assert retirement.validate_git_topology(heads, TAGS, commits, TREES, ANCHOR, 'michiflin-state.bin') == ANCHOR


def test_only_old_asset_ids_can_be_removed():
    assert retirement.validate_delete_ids({1, 2}, 3, 4, {5, 6}) == {1, 2, 3}
    with pytest.raises(retirement.RetirementError):
        retirement.validate_delete_ids({1, 4}, 3, 4, {5})
    with pytest.raises(retirement.RetirementError):
        retirement.validate_delete_ids({1, 5}, 3, 4, {5})
    with pytest.raises(retirement.RetirementError):
        retirement.validate_delete_ids({1}, 3, 4, set())


def test_force_update_is_forbidden_without_exact_repo_and_explicit_flag(monkeypatch):
    class Storage:
        repo = 'someone/public-repo'
    monkeypatch.setenv('MICHIFLIN_RETIRE_LEGACY', '1')
    with pytest.raises(retirement.RetirementError, match='authorization'):
        retirement.execute(Storage(), {})
    Storage.repo = retirement.ALLOWED_REPO
    monkeypatch.delenv('MICHIFLIN_RETIRE_LEGACY')
    with pytest.raises(retirement.RetirementError, match='authorization'):
        retirement.execute(Storage(), {})


def test_execute_rewrites_only_private_main_and_removes_only_old_assets_last_pinned(monkeypatch):
    from types import SimpleNamespace
    storage = SimpleNamespace(repo=retirement.ALLOWED_REPO, token='synthetic', release={'id': 100}, sealed=lambda: True)
    monkeypatch.setenv('MICHIFLIN_RETIRE_LEGACY', '1')
    current = {'sha': HEAD}
    assets = {1, 2, 3, 4, 5}
    calls = []
    plan = {'head': HEAD, 'anchor': ANCHOR, 'delete_ids': {1, 2, 3}, 'legacy_id': 3,
            'archive_id': 4, 'new_ids': {5}, 'asset_ids': set(assets),
            'heads': retirement._reference_summary(HEADS), 'tags': retirement._reference_summary(TAGS)}
    class Response:
        def __init__(self, value): self.value = value
        def json(self): return self.value
        def raise_for_status(self): return None
    monkeypatch.setattr(retirement.state_backup, '_assert_private', lambda *_: None)
    monkeypatch.setattr(retirement, '_list', lambda _storage, suffix, **_kw: HEADS if 'heads' in suffix else TAGS)
    monkeypatch.setattr(retirement, '_get', lambda *_a, **_kw: Response({'object': {'sha': current['sha']}}))
    monkeypatch.setattr(retirement.state_backup, '_assets', lambda *_: [{'id': n} for n in sorted(assets)])
    monkeypatch.setattr(retirement.state_backup, '_managed', lambda _item: False)
    def patch(url, *, json, **_kwargs):
        assert url.endswith('/YosvanyAlain/michiflin-private-state/git/refs/heads/main')
        assert json == {'sha': ANCHOR, 'force': True}
        calls.append('private-main')
        current['sha'] = ANCHOR
        return Response({})
    def delete(url, **_kwargs):
        asset_id = int(url.rsplit('/', 1)[-1])
        calls.append(asset_id)
        assets.remove(asset_id)
        return Response({})
    monkeypatch.setattr(retirement.requests, 'patch', patch)
    monkeypatch.setattr(retirement.requests, 'delete', delete)
    assert retirement.execute(storage, plan) == 3
    assert calls == ['private-main', 1, 2, 3]
    assert assets == {4, 5}


def test_execute_aborts_before_force_if_identity_seal_changed(monkeypatch):
    from types import SimpleNamespace
    storage = SimpleNamespace(repo=retirement.ALLOWED_REPO, token='synthetic', release={'id': 100}, sealed=lambda: False)
    monkeypatch.setenv('MICHIFLIN_RETIRE_LEGACY', '1')
    monkeypatch.setattr(retirement.state_backup, '_assert_private', lambda *_: None)
    monkeypatch.setattr(retirement, '_get', lambda *_a, **_kw: SimpleNamespace(json=lambda: {'object': {'sha': HEAD}}))
    monkeypatch.setattr(retirement.requests, 'patch', lambda *_a, **_kw: pytest.fail('must not rewrite'))
    with pytest.raises(retirement.RetirementError, match='refs changed'):
        retirement.execute(storage, {'head': HEAD})
