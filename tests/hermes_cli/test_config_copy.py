import copy
import datetime

from hermes_cli.config import DEFAULT_CONFIG, _copy_config


def test_config_copy_retains_aliases_cycles_and_uncommon_yaml_values():
    shared = [{'enabled': True}]
    original = {'a': shared, 'b': shared, 'timestamp': datetime.date(2026, 9, 8),
                'tags': {'one', 'two'}, 'tuple': (shared,)}
    original['self'] = original
    result = _copy_config(original)
    assert result is not original
    assert result['self'] is result
    assert result['a'] is result['b'] is result['tuple'][0]
    assert result['timestamp'] == original['timestamp']
    result['a'][0]['enabled'] = False
    result['tags'].add('three')
    assert original['a'][0]['enabled'] is True
    assert original['tags'] == {'one', 'two'}


def test_cached_config_copies_remain_independent(monkeypatch, tmp_path):
    from hermes_cli.config import load_config, load_config_readonly
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path/'config.yaml').write_text('model:\n  default: original\n')
    first = load_config()
    first['model']['default'] = 'changed'
    assert load_config()['model']['default'] == 'original'
    assert load_config_readonly()['model']['default'] == 'original'
    assert _copy_config(DEFAULT_CONFIG) == copy.deepcopy(DEFAULT_CONFIG)
