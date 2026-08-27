import secrets

from gi.repository import Gio, GLib


class PortalFileChooser:
    """Small xdg-desktop-portal FileChooser wrapper for GTK-independent picking."""

    BUS_NAME = "org.freedesktop.portal.Desktop"
    OBJECT_PATH = "/org/freedesktop/portal/desktop"
    INTERFACE = "org.freedesktop.portal.FileChooser"
    REQUEST_INTERFACE = "org.freedesktop.portal.Request"

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._subscriptions = {}

    def open_file(self, title, callback, multiple=False):
        token = f"quickvideo_{secrets.token_hex(6)}"
        options = {
            "multiple": GLib.Variant("b", bool(multiple)),
            "directory": GLib.Variant("b", False),
            "handle_token": GLib.Variant("s", token),
        }
        params = GLib.Variant("(ssa{sv})", ("", title, options))

        self.bus.call(
            self.BUS_NAME,
            self.OBJECT_PATH,
            self.INTERFACE,
            "OpenFile",
            params,
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_open_called,
            callback,
        )


    def save_file(self, title, callback, suggested_name='edited.mp4'):
        token = f"quickvideo_{secrets.token_hex(6)}"
        options = {
            "handle_token": GLib.Variant("s", token),
            "current_name": GLib.Variant("s", suggested_name),
        }
        params = GLib.Variant("(ssa{sv})", ("", title, options))

        self.bus.call(
            self.BUS_NAME,
            self.OBJECT_PATH,
            self.INTERFACE,
            "SaveFile",
            params,
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_open_called,
            callback,
        )

    def _on_open_called(self, bus, result, callback):
        try:
            reply = bus.call_finish(result)
            request_path = reply.unpack()[0]
        except Exception as exc:
            callback(None, exc)
            return

        subscription_id = self.bus.signal_subscribe(
            self.BUS_NAME,
            self.REQUEST_INTERFACE,
            "Response",
            request_path,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_response,
            (callback, request_path),
        )
        self._subscriptions[request_path] = subscription_id

    def _on_response(self, connection, sender_name, object_path,
                     interface_name, signal_name, parameters, user_data):
        callback, request_path = user_data
        subscription_id = self._subscriptions.pop(request_path, None)
        if subscription_id is not None:
            self.bus.signal_unsubscribe(subscription_id)

        response, results = parameters.unpack()
        if response != 0:
            callback(None, None)
            return

        uris = results.get("uris", [])
        if not uris:
            callback(None, RuntimeError("The file chooser returned no file."))
            return

        try:
            file = Gio.File.new_for_uri(uris[0])
            callback(file, None)
        except Exception as exc:
            callback(None, exc)
