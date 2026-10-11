"""Logical Host notification reader adoption, isolated from production link."""
import struct
from . import message as m


class SampleHostNotificationReader:
    def run(self, receive, clock, *, deadline, corr, expected):
        notifications, gaps = [], {fn: 0 for fn in expected}
        answer = None
        while clock() < deadline:
            frame = receive(deadline)
            if frame is None: continue
            if not frame: continue
            role = frame[0]
            if role == 2:
                if len(frame) < 5: raise ValueError('short result')
                if corr is not None and int.from_bytes(frame[1:3], 'little') == corr:
                    answer = frame
                    break
                continue
            if role not in (3, 4): continue
            if len(frame) < (6 if role == 3 else 15): raise ValueError('short notification')
            fn, seq = struct.unpack_from('<HH', frame, 1)
            if fn not in expected: continue
            # Count even an unknown event; event/data share this fn's sequence.
            gaps[fn] += (seq - expected[fn]) & 65535
            expected[fn] = (seq + 1) & 65535
            if role == 3:
                if frame[5] != 1: continue
                if len(frame) < 7: raise ValueError('short known event')
                tail = frame[7:]
            else:
                size = int.from_bytes(frame[13:15], 'little')
                if len(frame) < 15 + size: raise ValueError('short data')
                tail = frame[15 + size:]
            for tag, _ in m.split_tlvs(tail):
                if tag & 127 == 0: raise ValueError('zero TLV ID')
            notifications.append(frame)
        return {'reply': answer, 'notifications': notifications, 'expected': expected, 'gaps': gaps}
