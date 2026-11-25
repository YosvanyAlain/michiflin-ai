"""Offline-only counts from a synthetic release checkpoint, never contacts."""
import json

import pytest

from tools.diagnose_sync_snapshot import release_delivery_summary

SHA = 'a' * 40
EPOCH = 'b' * 48
BUILD = 'c' * 10


def record(states):
    return {'version': 1, 'revision': SHA, 'epoch': EPOCH, 'build': BUILD,
            'package_sha256': 'e' * 64, 'salt': 'f' * 64, 'phase': 'complete',
            'recipients': {f'{i:064x}': {'binding': f'{i + 100:064x}', 'state': status,
                                         'private': 'synthetic@example.invalid'}
                           for i, status in enumerate(states)}}


def data(value):
    return {'requested_release_deliveries': {'ux-update-20261119-v1': value}}


def test_counts_are_aggregate_only_and_not_read_receipts():
    source = record(['delivered', 'delivered', 'pending', 'uncertain', 'skipped'])
    got = release_delivery_summary(data(source), SHA, EPOCH, BUILD)
    assert got['release_ledger_present'] and got['release_ledger_valid'] and got['release_plan_complete']
    assert got['release_ledger_recipients'] == 5
    assert got['release_checkpointed_delivered'] == 2 and got['release_checkpointed_pending'] == 1
    assert got['release_checkpointed_uncertain'] == 1 and got['release_checkpointed_skipped'] == 1
    assert got['release_ledger_delivery_is_not_client_read_proof']
    encoded = json.dumps(got)
    for private in ('synthetic@example.invalid', 'f' * 64, 'e' * 64, '0' * 64):
        assert private not in encoded


def test_absence_is_not_safe_retry_evidence():
    got = release_delivery_summary({}, SHA, EPOCH, BUILD)
    assert got == {'release_ledger_present': False, 'release_ledger_valid': False,
                   'release_ledger_delivery_is_not_client_read_proof': True}


@pytest.mark.parametrize('mutation', ['revision', 'epoch', 'build', 'phase', 'recipient', 'status', 'binding', 'salt', 'non_string_state', 'non_string_phase'])
def test_bad_or_mismatched_plan_is_not_counted(mutation):
    value = record(['delivered'])
    entry = next(iter(value['recipients'].values()))
    if mutation in {'revision', 'epoch', 'build', 'phase', 'salt'}:
        value[mutation] = 'wrong'
    elif mutation == 'recipient':
        value['recipients'] = {'synthetic@example.invalid': entry}
    elif mutation == 'non_string_state':
        entry['state'] = []
    elif mutation == 'non_string_phase':
        value['phase'] = []
    else:
        entry['state' if mutation == 'status' else mutation] = 'bad'
    got = release_delivery_summary(data(value), SHA, EPOCH, BUILD)
    assert got['release_ledger_present'] and not got['release_ledger_valid']
    assert 'release_ledger_recipients' not in got and 'release_checkpointed_delivered' not in got
