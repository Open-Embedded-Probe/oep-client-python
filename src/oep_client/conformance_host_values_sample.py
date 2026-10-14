"""Example reader for actual revision-1 result shapes; no production Host binding."""
from . import message as m


def decode(frame, shape):
    result = m.Result.unpack(frame)
    out = {'successful': result.succeeded}
    if result.resolution not in (0, 1) or result.resolution == 1 and result.detail not in (0, 1, 2):
        return {'successful': False}
    if result.resolution == 0:
        out['reason'] = result.detail
        if result.detail == 4:
            tail = m.Tail.parse(result.payload)
            cause = tail.get(1)
            if cause is not None:
                if len(cause) != 1: raise ValueError('cause width')
                out['cause'] = cause[0]
        return out
    reader = m.Reader(result.payload)
    if shape == 'debug':
        done, status = reader.take('HB')
        out.update(done=done, status=status, values=reader.words(done))
        out['successful'] = out['successful'] and status == 0
    elif shape == 'scan':
        tried, count = reader.take('BB')
        entries = [list(reader.take('BHHI')) for _ in range(count)]
        out.update(tried=tried, entries=entries)
    elif shape == 'confirm':
        magic = reader.bytes(4)
        revision, maximum, window, inflight, boot = reader.take('BHIBI')
        if magic != b'OEP!' or revision != 1: raise ValueError('confirm')
        out.update(revision=revision, maximum=maximum, window=window, inflight=inflight, boot=boot)
    else:
        raise ValueError('explicit result shape required')
    reader.tail()
    return out
