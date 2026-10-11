"""Explicit revision-1 core result reader adoption, separate from production Host."""
import struct


OPS={'confirm':1,'list':2,'describe':3,'clock':4,'open':16,'end':17,'keepalive':18,'lock_state':19}
REASONS={1,2,3,4,6,7,8,9,10,11,12}


def decode(frame,op,*,described_fn=0):
    if op not in OPS.values() or len(frame)<5 or frame[0]!=2:raise ValueError('explicit core result context/header')
    corr,resolution,detail=struct.unpack_from('<HBB',frame,1);body=frame[5:];at=0
    def take(fmt):
        nonlocal at
        size=struct.calcsize('<'+fmt)
        if at+size>len(body):raise ValueError('short core fixed shape')
        values=struct.unpack_from('<'+fmt,body,at);at+=size
        return values
    def blob(n):
        nonlocal at
        if at+n>len(body):raise ValueError('short counted core value')
        value=body[at:at+n];at+=n;return value
    fields={};repeated=set();fixed={};successful=resolution==1 and detail==0
    if resolution not in (0,1) or resolution==1 and detail!=0 or resolution==0 and detail not in REASONS:
        return dict(corr=corr,successful=False,fixed=None,tlvs=[])
    if resolution==0:
        if detail==8:fixed['remaining_ms']=take('I')[0];fields={1:'owner'}
        elif detail==4:fields={1:1,2:2,5:2};repeated={2}
        elif detail==11:
            fixed['tag']=take('B')[0];fields={2:2,5:2};repeated={2}
            if op==1:fields[1]=2
    elif op==1:
        magic,revision,flags,maximum,window,inflight,boot=take('4sBBHIBI')
        if magic!=b'OEP!' or revision!=1:raise ValueError('unsupported confirm shape')
        fixed=dict(magic=magic,revision=revision,maximum=maximum,window=window,inflight=inflight,boot=boot);fields={1:1}
    elif op==2:
        total,count=take('HB');entries=[]
        for _ in range(count):
            fn,instance,revision,flags,n=take('HHBBB')
            if not 1<=n<=48:raise ValueError('interface name length')
            entries.append(dict(fn=fn,instance=instance,revision=revision,name=blob(n)))
        fixed=dict(total=total,entries=entries)
    elif op==3:
        fixed['more']=bool(take('B')[0]);fields={1:'role_channels',2:4,3:2,4:4,5:4,6:'channel_group',7:'ops'};repeated={1,6}
        if described_fn==0:
            fields.update({64:'text',65:'text',66:'unit_id',67:2,70:'label',73:3,76:'text',77:4});repeated|={70,73}
    elif op==4:fixed['boot'],fixed['uptime_ns']=take('IQ')
    elif op==16:fixed['lease_ms'],fixed['boot']=take('II')
    elif op==19:
        locked,remaining=take('BI');fixed=dict(locked=bool(locked),remaining_ms=remaining);fields={1:'owner'}
    seen=set();rows=[]
    while at<len(body):
        tag,n=take('BH');value=blob(n)
        if tag&127==0:raise ValueError('zero TLV ID')
        if tag&128 or tag not in fields or tag in seen and tag not in repeated:continue
        seen.add(tag);shape=fields[tag]
        valid=(len(value)==shape) if type(shape)is int else True
        if shape=='owner':valid=1<=n<=32
        elif shape=='unit_id':valid=1<=n<=32 and all(c in b'abcdefghijklmnopqrstuvwxyz0123456789-' for c in value)
        elif shape=='role_channels':valid=n>=3
        elif shape=='channel_group':valid=n>=2 and n==2+3*value[1]
        elif shape=='ops':valid=n>=2 and value[0]+8*(n-1)<=256
        elif shape=='label':valid=n>=2
        if not valid:raise ValueError('known core TLV shape')
        rows.append((tag,value))
    return dict(corr=corr,successful=successful,fixed=fixed,tlvs=rows)
