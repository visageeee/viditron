#!/usr/bin/env python3
import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

GLib.set_prgname("io.github.quickvideo.QuickVideo")
GLib.set_application_name("QuickVideo")

from quickvideo.application import main

raise SystemExit(main())
