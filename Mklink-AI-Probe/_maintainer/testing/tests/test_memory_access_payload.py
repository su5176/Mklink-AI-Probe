"""Regression coverage for a payload row identical to the dump header."""
import pytest
from mklink.memory_access import parse_read_ram_response

def dump_row(address, data):
    return f'{address:08X} ' + ' '.join(f'{value:02x}' for value in data) + ' '

@pytest.mark.parametrize('ascending_row', [0, 1, 40, 63])
def test_header_like_firmware_row_is_preserved(ascending_row):
    rows=[bytes([0xA5])*16 for _ in range(64)]
    rows[ascending_row]=bytes(range(16))
    response='cmd.read_ram(0x08000000, 1024)\r\n'
    response+='7AC3FBA9 00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F\r\n'
    response+='\r\n'.join(dump_row(0x08000000+i*16,row) for i,row in enumerate(rows))
    response+='\r\n0\r\n>>> '
    assert parse_read_ram_response(response)==b''.join(rows)

def test_headerless_payload_preserves_later_ascending_row():
    first=bytes([0xA5])*16
    assert parse_read_ram_response(dump_row(0x20000000,first)+'\n'+dump_row(0x20000010,bytes(range(16))))==first+bytes(range(16))

def test_partial_last_row_and_empty_response():
    response='12345678 00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F\n'
    response+=dump_row(0x20000000,bytes([0x42])*12)
    assert parse_read_ram_response(response)==bytes([0x42])*12
    assert parse_read_ram_response('probe error\n>>> ')==b''
