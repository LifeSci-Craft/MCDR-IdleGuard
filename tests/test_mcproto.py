"""mcproto 模块的基础类型与解析逻辑测试。"""
from idle_guard.mcproto import (
    component_to_text,
    decode_varint,
    encode_string,
    encode_varint,
    pack_packet,
    parse_string,
)


def test_varint_roundtrip():
    for value in (0, 1, 2, 127, 128, 255, 300, 25565, 2 ** 31 - 1, -1):
        data = encode_varint(value)
        decoded, offset = decode_varint(data, 0)
        assert offset == len(data)
        assert decoded == (value & 0xFFFFFFFF), f'{value} 编码后解析为 {decoded}'


def test_varint_known_bytes():
    # 协议里最常用的几个取值，防止实现漂移
    assert encode_varint(0) == b'\x00'
    assert encode_varint(1) == b'\x01'
    assert encode_varint(127) == b'\x7f'
    assert encode_varint(128) == b'\x80\x01'
    assert encode_varint(25565) == b'\xdd\xc7\x01'


def test_string_roundtrip():
    for text in ('', 'Steve', '中文玩家', 'a' * 300):
        data = encode_string(text)
        decoded, offset = parse_string(data, 0)
        assert decoded == text
        assert offset == len(data)
        # VarInt 长度前缀应当等于 UTF-8 字节数
        length, _ = decode_varint(data, 0)
        assert length == len(text.encode('utf-8'))


def test_packet_layout():
    # 包 = [长度][包 ID][负载]；长度只覆盖「包 ID + 负载」
    packet = pack_packet(0x00, b'\x01\x02\x03')
    assert packet == bytes([0x04, 0x00, 0x01, 0x02, 0x03])
    assert pack_packet(0x01) == bytes([0x01, 0x01])


def test_component_to_text():
    assert component_to_text('hello') == 'hello'
    assert component_to_text({'text': 'a', 'extra': [{'text': 'b'}, {'text': 'c'}]}) == 'abc'
    assert component_to_text([{'text': 'x'}, 'y']) == 'xy'
    assert component_to_text({'translate': 'chat.type.text', 'with': [{'text': 'Steve'}]}) == 'chat.type.textSteve'
    assert component_to_text(None) == ''
