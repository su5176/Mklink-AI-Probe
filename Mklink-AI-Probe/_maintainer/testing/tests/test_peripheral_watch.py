from pathlib import Path
import asyncio
import struct
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from mklink.peripheral_watch import SvdTarget, pdsc_targets, svd_watch_items
from mklink.superwatch import WatchItem, SuperWatchRuntime, build_read_blocks, compile_frame_decoder
from mklink.remote.dashboards import SuperWatchStreamManager


SVD = b'''<device><name>TEST</name><version>1</version><description>test</description>
<addressUnitBits>8</addressUnitBits><width>32</width><size>32</size><access>read-write</access>
<peripherals><peripheral><name>GPIOA</name><baseAddress>0x40010800</baseAddress><registers>
<register><name>IDR</name><addressOffset>8</addressOffset><access>read-only</access><fields>
<field><name>IDR0</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
<field><name>IDR12</name><bitOffset>12</bitOffset><bitWidth>1</bitWidth></field>
</fields></register>
<register><name>ODR</name><addressOffset>12</addressOffset></register>
<register><name>CLEAR</name><addressOffset>16</addressOffset><readAction>clear</readAction></register>
<register><name>WRITE</name><addressOffset>20</addressOffset><access>write-only</access></register>
<register><name>FIELD_CLEAR</name><addressOffset>24</addressOffset><fields><field><name>FLAG</name>
<bitOffset>0</bitOffset><bitWidth>1</bitWidth><readAction>clear</readAction></field></fields></register>
</registers></peripheral>
<peripheral derivedFrom="GPIOA"><name>GPIOB</name><baseAddress>0x40010c00</baseAddress></peripheral>
</peripherals></device>'''


def test_svd_inheritance_aliases_and_side_effect_filter():
    items, skipped = svd_watch_items(SVD)
    assert items['GPIOB.12'].address == 0x40010C08
    assert items['GPIOB.12'].metadata['writable'] is False
    assert items['GPIOB.IDR.IDR12'].metadata['bit_offset'] == 12
    assert items['GPIOB.ODR'].size == 4
    assert not any('CLEAR' in name or 'WRITE' in name for name in items)
    assert skipped == 6


def test_peripheral_bits_share_register_read_and_preserve_ram_decoding():
    catalog, _ = svd_watch_items(SVD)
    watches = [catalog['GPIOB.0'], catalog['GPIOB.12'], catalog['GPIOB.IDR'],
               WatchItem('temperature', 0x20000000, 'float', 4, scalar_kind='float')]
    blocks = build_read_blocks(watches, max_gap=0)
    assert [(block.address, block.size) for block in blocks] == [(0x20000000, 4), (0x40010C08, 4)]
    decoder = compile_frame_decoder(watches, blocks)
    assert decoder.decode({'regions': [(0, struct.pack('<f', 25.5)), (1, struct.pack('<I', 0x1001))]}) == [1, 1, 0x1001, 25.5]
    assert decoder.decode({'regions': [(0, struct.pack('<f', -2.5)), (1, struct.pack('<I', 2))]}) == [0, 0, 2, -2.5]
    assert decoder.decode({'regions': [(0, b'\0')]}) is None


def test_pdsc_selects_exact_device_and_inherited_debug_path(tmp_path):
    data = b'''<package xmlns="test"><vendor>Vendor</vendor><name>DFP</name><devices>
    <family Dfamily="family"><debug svd="SVD/base.svd"/><device Dname="CHIP_A"/>
    <device Dname="CHIP_B"><debug svd="SVD/other.svd"/><variant Dvariant="CHIP_B1"/></device>
    <device Dname="BAD"><debug svd="../outside.svd"/></device></family></devices></package>'''
    targets = pdsc_targets(data, tmp_path / 'test.pdsc')
    assert [(t.target, t.svd) for t in targets] == [('CHIP_A', 'SVD/base.svd'), ('CHIP_B', 'SVD/other.svd'), ('CHIP_B1', 'SVD/other.svd')]
    assert len({t.key for t in targets}) == 3


def test_svd_loads_from_pack_without_extraction(tmp_path):
    path = tmp_path / 'test.pack'
    with ZipFile(path, 'w') as pack:
        pack.writestr('SVD/test.svd', SVD)
    target = SvdTarget('CHIP', 'Vendor.DFP@1', path, 'SVD/test.svd', True)
    assert target.read() == SVD
    with pytest.raises(ValueError):
        SvdTarget('CHIP', 'bad', path, '../escape.svd', True).read()


def test_svd_does_not_accept_entities():
    with pytest.raises(ValueError):
        svd_watch_items(b'<!DOCTYPE a [<!ENTITY x "value">]><device/>')


def test_device_discovery_uses_pdsc_not_first_svd_in_directory(tmp_path, monkeypatch):
    from mklink.peripheral_watch import discover_svd_targets
    from mklink.project_config import save_project_info
    pack = tmp_path / 'packs' / 'Vendor' / 'DFP' / '1.0'
    (pack / 'Flash').mkdir(parents=True)
    (pack / 'SVD').mkdir()
    (pack / 'SVD' / 'right.svd').write_bytes(SVD)
    (pack / 'SVD' / 'wrong.svd').write_bytes(SVD)
    (pack / 'Vendor.DFP.pdsc').write_text('''<package><vendor>Vendor</vendor><name>DFP</name><devices>
    <family Dfamily="TEST"><device Dname="CHIP"><debug svd="SVD/right.svd"/></device></family>
    </devices></package>''')
    save_project_info(str(tmp_path), {'flm_path': str(pack / 'Flash' / 'algorithm.FLM')})
    monkeypatch.setattr('mklink.cmsis_dap.algorithm_catalog._installed_pack_records', lambda *args: [])
    monkeypatch.setattr('mklink.cmsis_dap.builtin_pack_bundle.load_builtin_pack_records', lambda: [])
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'empty')
    monkeypatch.delenv('ARM_PACK_ROOT', raising=False)
    monkeypatch.delenv('CMSIS_PACK_ROOT', raising=False)
    targets = discover_svd_targets(str(tmp_path))
    assert [(target.target, target.svd) for target in targets] == [('CHIP', 'SVD/right.svd')]


def test_chip_selection_needs_no_axf_and_preserves_program_watches(tmp_path):
    path = tmp_path / 'test.svd'
    path.write_bytes(SVD)
    target = SvdTarget('CHIP', 'Vendor.DFP@1', tmp_path / 'test.pdsc', path.name)
    device = SimpleNamespace(_project_root=str(tmp_path), symbol_catalog=None)
    manager = SuperWatchStreamManager()
    manager.prepare(device)
    ram = WatchItem('counter', 0x20000000, 'uint32_t', 4)
    manager._runtime.items.append(ram)
    result = manager.select_peripherals(device, target)
    assert result['selection']['target'] == 'CHIP'
    assert manager.add_watch('GPIOB.12')['item']['source'] == 'peripheral'
    assert manager._runtime.items[0] is ram
    manager._running = True
    manager._stop_event.clear()
    with pytest.raises(RuntimeError, match='Stop SuperWatch'):
        manager.select_peripherals(device, target)
    assert manager._runtime.items[0] is ram
    manager._running = False
    manager.prepare(SimpleNamespace(_project_root=str(tmp_path), symbol_catalog=None))
    assert manager.peripheral_catalog()['items'] == []
    assert [item.name for item in manager._runtime.items] == ['counter']


def test_peripheral_api_requires_a_discovered_chip_and_preserves_active_capture(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from mklink.remote.api import create_app
    from route_utils import find_route

    (tmp_path / 'chip.svd').write_bytes(SVD)
    target = SvdTarget('CHIP', 'Vendor.DFP@1', tmp_path / 'test.pdsc', 'chip.svd')
    monkeypatch.setattr('mklink.peripheral_watch.discover_svd_targets', lambda root: [target])
    manager = SuperWatchStreamManager()
    managers = {name: SimpleNamespace() for name in ('systemview', 'rtt', 'vofa', 'serial', 'modbus')}
    managers['superwatch'] = manager
    monkeypatch.setattr('mklink.remote.dashboards.get_managers', lambda: managers)
    app = create_app(auth_token=None, project_root=str(tmp_path))
    device = SimpleNamespace(connected=True, _project_root=str(tmp_path), symbol_catalog=None)
    app.state.mklink_state['device'] = device
    targets = find_route(app, '/api/dash/superwatch/peripherals/targets').endpoint
    select = find_route(app, '/api/dash/superwatch/peripherals/select').endpoint

    async def verify():
        with pytest.raises(HTTPException) as error:
            await select(target_id='not-listed')
        assert error.value.status_code == 404
        assert (await targets(q='CHIP'))['targets'][0]['target'] == 'CHIP'
        assert (await select(target_id=target.key))['selection']['target'] == 'CHIP'
        manager._running = True
        manager._stop_event.clear()
        with pytest.raises(HTTPException) as error:
            await select(target_id=target.key)
        assert error.value.status_code == 409
        assert manager.running

    asyncio.run(verify())
