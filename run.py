#!/usr/bin/env python3
import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

GLib.set_prgname("io.github.visageeee.Viditron")
GLib.set_application_name("Viditron")

from viditron.application import main

raise SystemExit(main())
