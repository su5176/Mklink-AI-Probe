import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mklink.dwarf_parser import DwarfInfo, DwarfVariable
from mklink.remote.api import create_app
from mklink.symbol_catalog import SymbolCatalog
from mklink.watch_preferences import PreferencesConflict, load_pins, save_pins


def catalog_for(path, *, address=0x20000010, generation=1, gain=True):
    info = DwarfInfo(base_types={1: ('float', 4)}, variables={
        'temperature': DwarfVariable('temperature', 11, 1, 0x20000024, 4, 'float'),
    })
    if gain:
        info.variables['gain'] = DwarfVariable('gain', 10, 1, address, 4, 'float')
    return SymbolCatalog.from_dwarf(info, axf_path=str(path), generation=generation,
                                    ram_ranges=[(0x20000000, 0x20010000)])


def test_pin_names_order_persist_and_are_project_isolated(tmp_path):
    first, second = str(tmp_path / 'first'), str(tmp_path / 'second')
    state = save_pins(first, ['temperature', 'gain', 'gain'], load_pins(first)['revision'])
    assert state['pins'] == ['temperature', 'gain']
    assert load_pins(first) == state
    assert load_pins(second)['pins'] == []
    raw = json.loads((tmp_path / 'first/.mklink/superwatch_pins.json').read_text())
    assert raw == {'version': 1, 'pins': ['temperature', 'gain']}
    with pytest.raises(PreferencesConflict):
        save_pins(first, [], load_pins(second)['revision'])
    assert load_pins(first) == state


@pytest.mark.parametrize('pins', [['x'] * 129, [''], ['bad\nname'], ['x' * 513], [123], {}])
def test_invalid_preferences_do_not_write(tmp_path, pins):
    with pytest.raises(ValueError):
        save_pins(str(tmp_path), pins, load_pins(str(tmp_path))['revision'])
    assert not (tmp_path / '.mklink').exists()


def test_corrupt_preferences_are_not_silently_overwritten(tmp_path):
    folder = tmp_path / '.mklink'
    folder.mkdir()
    path = folder / 'superwatch_pins.json'
    path.write_text('{broken')
    with pytest.raises(ValueError):
        save_pins(str(tmp_path), ['gain'], '')
    assert path.read_text() == '{broken'


def test_atomic_write_failure_preserves_previous_names(tmp_path, monkeypatch):
    state = save_pins(str(tmp_path), ['gain'], load_pins(str(tmp_path))['revision'])
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr('mklink.watch_preferences.os.replace', fail)
    with pytest.raises(OSError):
        save_pins(str(tmp_path), [], state['revision'])
    assert load_pins(str(tmp_path)) == state
    assert len(list((tmp_path / '.mklink').iterdir())) == 1


def test_pins_api_rebinds_addresses_and_preserves_missing_names(tmp_path):
    axf = tmp_path / 'app.axf'
    axf.write_bytes(b'axf')
    device = SimpleNamespace(connected=False, symbol_catalog=catalog_for(axf))
    app = create_app(auth_token=None, project_root=str(tmp_path))
    app.state.mklink_state['device'] = device
    client = TestClient(app)
    initial = client.get('/api/dash/superwatch/pins').json()
    saved = client.put('/api/dash/superwatch/pins', json={
        'pins': ['gain', 'not_in_this_build'], 'revision': initial['revision'],
    })
    assert saved.status_code == 200
    assert saved.json()['entries'][0]['descriptor']['address'] == 0x20000010
    assert saved.json()['entries'][1]['descriptor'] is None
    assert client.put('/api/dash/superwatch/pins', json={
        'pins': [], 'revision': initial['revision'],
    }).status_code == 409
    device.symbol_catalog = catalog_for(axf, address=0x20000040, generation=2)
    assert client.get('/api/dash/superwatch/pins').json()['entries'][0]['descriptor']['address'] == 0x20000040
    device.symbol_catalog = catalog_for(axf, gain=False, generation=3)
    missing = client.get('/api/dash/superwatch/pins').json()
    assert missing['pins'][0] == 'gain' and missing['entries'][0]['descriptor'] is None
    axf.write_bytes(b'changed')
    assert all(entry['descriptor'] is None for entry in client.get('/api/dash/superwatch/pins').json()['entries'])
    # A new backend instance reads the project preferences without connecting a probe.
    new_client = TestClient(create_app(auth_token=None, project_root=str(tmp_path)))
    assert new_client.get('/api/dash/superwatch/pins').json()['pins'] == ['gain', 'not_in_this_build']


def test_multi_keyword_search_matches_any_term_and_deduplicates(tmp_path):
    axf = tmp_path / 'app.axf'
    axf.write_bytes(b'axf')
    catalog = catalog_for(axf)
    assert [x.path for x in catalog.search('gain, temperature，gain;missing')] == ['gain', 'temperature']
    assert {x.path for x in catalog.search('AIN；temp')} == {'gain', 'temperature'}
    assert [x.path for x in catalog.search('gain, temperature', limit=1)] == ['gain']
