"""Example Host decoder adoption; not installed into the production Host."""
from . import message as m


def decode(payload):
    reader = m.Reader(payload)
    fixed = reader.u16()
    blob = reader.bytes(reader.u16())
    rows = m.split_tlvs(reader.rest())
    first, repeated = None, []
    for tag, value in rows:
        if tag & 127 == 0:
            raise ValueError('zero TLV ID')
        if tag & 128 or tag not in (1, 3):
            continue
        if len(value) != 1:
            raise ValueError('known u8 TLV width')
        if tag == 1 and first is None:
            first = value[0]
        elif tag == 3:
            repeated.append(value[0])
    return {'fixed': fixed, 'blob': blob, 'first': first, 'repeated': repeated}
