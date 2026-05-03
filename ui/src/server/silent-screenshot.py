#!/usr/bin/env python3
"""
Silent screenshot helper for the host-control MCP server.

GNOME Mutter restricts org.gnome.Shell.Screenshot.Screenshot to callers
holding the well-known bus name `org.gnome.Screenshot` (the same name
gnome-screenshot acquires). The DBus method takes a `flash` bool — when
false, GNOME Shell skips the flash overlay AND the shutter sound, which
is what we want for an autonomous agent.

Usage: silent-screenshot.py <output.png>
Exits 0 on success; prints the saved path to stdout.
"""
import sys
from gi.repository import Gio, GLib

if len(sys.argv) != 2:
    print('usage: silent-screenshot.py <output.png>', file=sys.stderr)
    sys.exit(2)

output = sys.argv[1]
loop = GLib.MainLoop()
result = {'ok': False, 'err': None, 'used': None}

def on_acquired(conn, name):
    try:
        ret = conn.call_sync(
            'org.gnome.Shell',
            '/org/gnome/Shell/Screenshot',
            'org.gnome.Shell.Screenshot',
            'Screenshot',
            GLib.Variant('(bbs)', (False, False, output)),
            None, Gio.DBusCallFlags.NONE, 5000, None
        )
        success, used = ret.unpack()
        result['ok'] = bool(success)
        result['used'] = used
    except Exception as e:
        result['err'] = str(e)
    loop.quit()

def on_lost(conn, name):
    if not result['ok'] and not result['err']:
        result['err'] = f'failed to acquire bus name {name}'
        loop.quit()

Gio.bus_own_name(
    Gio.BusType.SESSION,
    'org.gnome.Screenshot',
    Gio.BusNameOwnerFlags.REPLACE,
    None, on_acquired, on_lost
)

GLib.timeout_add_seconds(8, lambda: (result.update({'err': 'timeout'}), loop.quit()))
loop.run()

if result['ok']:
    print(result['used'] or output)
    sys.exit(0)
print(result['err'] or 'unknown error', file=sys.stderr)
sys.exit(1)
