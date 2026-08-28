import sys
import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Gio

from .window import MainWindow


class ViditronApplication(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.visageeee.Viditron",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        # GTK/X11 uses this as the desktop/WM identity. It must match the
        # StartupWMClass in viditron.desktop so docks group the running
        # window with the installed launcher rather than showing "run.py".
        self.set_resource_base_path("/io/github/visageeee/Viditron")

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = MainWindow(application=self)
        window.present()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        window = self.props.active_window
        if files:
            window.load_file(files[0].get_path())


def main():
    app = ViditronApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
