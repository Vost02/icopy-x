"""rtc_sync -- set the Linux system clock from the GD32 RTC at boot.

The iCopy-X keeps its real-time clock in the GD32 MCU (battery-backed).
The Linux side, however, never reads it, so the system clock starts at a
fixed fake date (2016-02-11) on every boot and every recorded file
timestamp is wrong.

This module asks the GD32 for its RTC via the HMI serial link
(``givemetime`` -> ``#rtctime:<epoch>``) and, if the answer looks sane,
sets the Linux clock from it.

Safety:
    * Everything is best-effort: any error is swallowed.
    * ``start()`` only spawns a daemon thread; it never blocks boot.
    * Callers should still import it inside a try/except (see main.py)
      so that even a broken module cannot prevent the app from booting.
"""

import logging
import os
import re
import struct
import threading
import time

logger = logging.getLogger(__name__)

_RTC_RE = re.compile(r'#rtctime:\s*(\d+)')
_LOG_PATH = '/mnt/upan/rtc_sync.log'

# Sanity window: only accept times between 2000-01-01 and 2100-01-01.
_MIN_EPOCH = 946684800
_MAX_EPOCH = 4102444800


def _log(msg):
    """Best-effort diagnostic log on the PC-Mode-visible partition."""
    try:
        with open(_LOG_PATH, 'a') as f:
            f.write(msg + '\n')
    except Exception:
        pass


def _read_rtc(timeout=1.5):
    """Send 'givemetime' and return the raw value, or None.

    Registers a temporary com-readback callback and restores whatever was
    there before, so it does not disturb other users of the serial link.
    """
    try:
        import hmi_driver
    except Exception:
        return None

    got = {}
    ev = threading.Event()

    def _on_line(line):
        try:
            m = _RTC_RE.search(line)
            if m:
                got['v'] = m.group(1)
                ev.set()
        except Exception:
            pass

    try:
        prev = getattr(hmi_driver, '_com_readback', None)
        hmi_driver.SetComReadBack(_on_line)
    except Exception:
        return None

    try:
        hmi_driver._set_com('givemetime')
        if ev.wait(timeout):
            return got.get('v')
        return None
    except Exception:
        return None
    finally:
        try:
            hmi_driver.SetComReadBack(prev)
        except Exception:
            pass


def _to_epoch(raw):
    """Convert the GD32 value to epoch seconds, or None if not sane.

    Handles both seconds (10 digits) and milliseconds (13 digits).
    """
    if not raw:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n > 99999999999:          # 12+ digits -> milliseconds
        n //= 1000
    if n < _MIN_EPOCH or n > _MAX_EPOCH:
        return None
    return n


def set_rtc(epoch):
    """Set the GD32 RTC to *epoch* (unix seconds). Best-effort; returns bool.

    Protocol (STM32 firmware ``cli_setrtc`` in sys_command_line.c):
        giveyoutime  T(0x54)  <4-byte big-endian epoch>  A(0x41)  \r\n

    Written as RAW BYTES: hmi_driver._set_com() utf-8 encodes the string,
    which would corrupt the four time bytes, so we write to _ser directly.
    """
    try:
        import hmi_driver
        ser = getattr(hmi_driver, '_ser', None)
        if ser is None or not getattr(ser, 'is_open', False):
            _log('set_rtc: no serial')
            return False
        frame = (b'giveyoutime' + b'T'
                 + struct.pack('>I', int(epoch) & 0xFFFFFFFF) + b'A' + b'\r\n')
        ser.write(frame)
        ser.flush()
        _log('set_rtc: ok %d' % int(epoch))
        return True
    except Exception as e:
        _log('set_rtc: failed: %s' % e)
        return False


def sync_now(timeout=1.5):
    """Read the GD32 RTC and set the Linux clock. Returns epoch or None."""
    raw = _read_rtc(timeout)
    secs = _to_epoch(raw)
    if secs is None:
        _log('boot rtc_sync: no usable RTC (raw=%r)' % (raw,))
        return None
    try:
        os.system('date -s "@%d"' % secs)
        _log('boot rtc_sync: ok raw=%r -> %d' % (raw, secs))
        return secs
    except Exception as e:
        _log('boot rtc_sync: date failed: %s' % e)
        return None


def start(delay=3.0):
    """Spawn a daemon thread that syncs once, shortly after boot.

    Returns True if the thread was started.  Never raises.
    """
    def _run():
        try:
            time.sleep(delay)
            sync_now()
        except Exception:
            pass

    try:
        t = threading.Thread(target=_run, daemon=True, name='rtc_sync')
        t.start()
        return True
    except Exception:
        return False
