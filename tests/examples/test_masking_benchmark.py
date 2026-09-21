"""Keeps benchmarks/masking.py, which docs/masking.md's speed figures come
from, from rotting: every policy and every masker on a small table, each
copy of the second policy required identical.
"""
import importlib.util
from pathlib import Path

import pytest

from bauta.masking import nativeVersion

BENCHMARK = Path(__file__).resolve().parents[2] / 'benchmarks' / 'masking.py'


@pytest.mark.skipif(nativeVersion() is None, reason='the benchmark measures the bauta_rs extension, which is not installed')
def test_the_benchmark_runs_every_policy_and_masker_to_the_same_copy(tmp_path, capsys):
    specification = importlib.util.spec_from_file_location('masking_benchmark', BENCHMARK)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    results = module.main(rows=300, python=True, workingDirectory=tmp_path)

    assert [policy for policy, _ in results['policies']] == ['cheap', 'key', 'fpe', 'keys']
    assert [label for label, _ in results['maskers']] == [label for label, *_ in module.MASKERS]
    assert all(result['rows'] == 300 for _, result in results['policies'] + results['maskers'])
    assert results['maskers'][0][1]['masker'] == 'python'
    assert 'Every copy of the second policy is identical' in capsys.readouterr().out
