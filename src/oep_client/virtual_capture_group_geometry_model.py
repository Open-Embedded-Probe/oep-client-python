"""Finite raw observation fixtures; not an acquisition engine or hardware adapter.

Builds controlled one-shot records of an immediate group start. The checker
does not import this module or use its expected records.
"""
import struct

from . import registry as _reg

_A = _reg.FIXTURE_ANALOG.tlv["configure_answer"]           # the capture family's answer tags (capture §3.3)


def tlv(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value


def geometry(*,rates=((1000,1),(333,1)),start_ns=20000000,offsets=(0,0),samples=(32,32),members=(2,3),index=0xffffffff):
    if len(rates)!=len(offsets) or len(rates)!=len(samples) or len(rates)!=len(members):raise ValueError('matching explicit track arrays required')
    configs={};records={};generation=11
    for at,(fn,words,offset,count) in enumerate(zip(members,rates,offsets,samples)):
        records[fn]=struct.pack('<IQIQIIBI',0,0,count,start_ns+offset,0,index,0,21+at)
        body=tlv(_A['actual_rate'],struct.pack('<II',*words))+tlv(_A['actual_samples'],struct.pack('<I',count))
        analog=at%2==0
        if analog:
            body+=tlv(_A['layout'],bytes((16,0,12,2,1,3)))+tlv(_A['reference'],struct.pack('<BIB',2,3300,0))
            for role in (1,3):body+=tlv(_A['scale'],struct.pack('<Bii',role,0,1000000))+tlv(_A['frontend_used'],bytes((role,0)))
        else:body+=tlv(_A['layout'],b'\3\2\0\1')
        configs[fn]=('oep.fixture.analog' if analog else 'oep.fixture.logic',body)
    start=struct.pack('<QIB',start_ns,generation,len(members))+b''.join(struct.pack('<HI',fn,21+at) for at,fn in enumerate(members))
    status=struct.pack('<BQI',4,start_ns,generation)
    return dict(configure_payloads=configs,start_payload=start,status_payload=status,first_segments=records,members=members,
                sample_origins={fn:start_ns+offset for fn,offset in zip(members,offsets)},timestamp_tolerance_ns=1)


def fixtures():
    result=[]
    for rates in [((1000,1),(333,1)),((333,1),(1000,1)),((1001,3),(83333,8)),((20000000,1),(48000,1))]:
        for offsets in [(0,0),(0,5000),(125000,0)]:
            for start_ns in [0,20000000]:
                result.append(geometry(rates=rates,offsets=offsets,start_ns=start_ns,samples=(65536,65536)))
    result.append(geometry(index=0))
    result.append(geometry(rates=((0xffffffff,0xfffffffd),(0xfffffffe,0xfffffffb)),start_ns=0xfffffffffffffff0,samples=(0xffffffff,0xffffffff)))
    result.append(geometry(rates=((1000,1),(333,1),(1001,3)),offsets=(0,5000,7000),samples=(64,64,64),members=(15,2,9)))
    return result
