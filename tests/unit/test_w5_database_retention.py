import json
from types import SimpleNamespace

import pytest

from evaluation.experiments.run_recovery_matrix import cleanup_project


def test_retention_never_removes_database_volumes(tmp_path):
    commands = []
    config = tmp_path / 'private.json'
    config.write_text('{}')

    def docker(*args):
        commands.append(args)
        if args[:2] == ('volume', 'ls'):
            return 'w5_postgres\nw5_edge'
        if args[:2] == ('volume', 'inspect'):
            return json.dumps([{'Labels': {'com.docker.compose.project': 'w5'}}])
        return ''

    runner = SimpleNamespace(docker=docker, out={},
                             compose_call=lambda *args: commands.append(args))
    cleanup_project(runner, 'w5', config, {'team_database'}, True)
    assert runner.out['retained_database_volumes'] == ['w5_edge', 'w5_postgres']
    assert not any(c[:2] == ('volume', 'rm') for c in commands)
    assert ('w5', config, 'down', '--remove-orphans') in commands


def test_retention_rejects_any_original_database_before_cleanup(tmp_path):
    commands = []
    runner = SimpleNamespace(docker=lambda *args: 'team_database', out={},
                             compose_call=lambda *args: commands.append(args))
    with pytest.raises(AssertionError):
        cleanup_project(runner, 'w5', tmp_path / 'private.json', {'team_database'}, True)
    assert not commands
