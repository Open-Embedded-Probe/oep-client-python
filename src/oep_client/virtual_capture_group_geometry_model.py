"""Finite raw observation fixtures; not an acquisition engine or hardware adapter.

Builds controlled one-shot records using exact arithmetic and a selected nearest
sample. The checker does not import this module or use its expected records.
"""
import struct
from fractions import Fraction


def tlv(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value


def geometry(*,rates=((1000,1),(333,1)),pre=2,trigger_ns=20000000,origins=(0,0),samples=(32,32),ties='lower',forced=False,members=(2,3)):
    if len(rates)!=len(origins) or len(rates)!=len(samples) or len(rates)!=len(members):raise ValueError('matching explicit track arrays required')
    configs={};records={};source=members[0];start_ns=min(origins);generation=11
    src=Fraction(*rates[0])
    for at,(fn,words,origin,count) in enumerate(zip(members,rates,origins,samples)):
        rate=Fraction(*words);period=Fraction(1000000000)/rate
        # Fraction rounding and an explicit tie policy, rather than checker candidate enumeration.
        coordinate=Fraction(trigger_ns-origin)/period
        if coordinate<=0:closest=0
        else:
            whole,remainder=divmod(coordinate.numerator,coordinate.denominator)
            closest=whole+int(2*remainder>coordinate.denominator or (2*remainder==coordinate.denominator and ties=='upper'))
        duration=pre*rate/src
        wanted=(duration.numerator+duration.denominator-1)//duration.denominator
        if wanted>=count:raise ValueError('fixture pretrigger must fit actual capacity')
        history=min(wanted,closest);begin=closest-history;actual=count-(wanted-history)
        ns=int(Fraction(origin)+begin*period);flags=2 if actual<count else 0;track_generation=21+at
        records[fn]=struct.pack('<IQIQIIBI',0,0,actual,ns,0,history,flags,track_generation)
        body=tlv(80,struct.pack('<II',*words))+tlv(82,struct.pack('<I',count))+tlv(86,b'\0'*4)
        analog=at%2==0
        if analog:
            body+=tlv(81,bytes((16,0,12,2,1,3)))+tlv(89,struct.pack('<BIB',2,3300,0))
            for role in (1,3):body+=tlv(85,struct.pack('<Bii',role,0,1000000))+tlv(88,bytes((role,0)))
        else:body+=tlv(81,b'\3\2\0\1')
        configs[fn]=('oep.fixture.analog' if analog else 'oep.fixture.logic',body)
    start=struct.pack('<IQIB',0,start_ns,generation,len(members))+b''.join(struct.pack('<HI',fn,21+at) for at,fn in enumerate(members))
    status=struct.pack('<BQQHI',4,start_ns,trigger_ns,0 if forced else source,generation)
    return dict(configure_payloads=configs,start_payload=start,status_payload=status,first_segments=records,members=members,source_fn=source,pretrigger=pre,sample_origins=dict(zip(members,origins)),expected_trigger_ns=trigger_ns,forced=forced,max_op_ms=1000,timestamp_tolerance_ns=1,continuous_history=True)


def fixtures():
    result=[]
    for rates in [((1000,1),(333,1)),((333,1),(1000,1)),((1001,3),(83333,8)),((20000000,1),(48000,1))]:
        for pre in [0,2,7]:
            for trigger in [0,1000000,50000000]:
                # This metadata-only fixture needs no array proportional to the capacity.
                result.append(geometry(rates=rates,pre=pre,trigger_ns=trigger,samples=(65536,65536)))
    for ties in ['lower','upper']:
        result.append(geometry(rates=((1000,1),(500,1)),pre=1,trigger_ns=5000000,ties=ties))
    result.append(geometry(origins=(5000,2000000),trigger_ns=1000000,pre=3,forced=True))
    result.append(geometry(rates=((0xffffffff,0xfffffffd),(0xfffffffe,0xfffffffb)),pre=0x7fffffff,trigger_ns=10000000000,samples=(0xffffffff,0xffffffff)))
    result.append(geometry(rates=((1000,1),(333,1),(1001,3)),origins=(0,5000,7000),samples=(64,64,64),pre=7,trigger_ns=50000000,members=(15,2,9)))
    return result
