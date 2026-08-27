# QuickVideo

GTK4 prototype for quick FFmpeg video edits.

## Ubuntu / Debian dependencies

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 ffmpeg \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav
```

## Run

```bash
python3 run.py
```

Or open a video directly:

```bash
python3 run.py /path/to/video.mp4
```

## Current crop / trim / rotate prototype

- Crop section
  - Define trim area button
  - draggable crop rectangle in the video preview
  - corner and edge handles
  - X / Y / Width / Height exact pixel fields
- Trim section
  - Define trim button
  - draggable in/out handles below the video controls
  - exact start/end timecode fields
- Rotate section
  - rotate left/right
  - horizontal/vertical flip
- Crop and Trim update one action each in the action queue

## Current editing interaction

- Crop handles are always shown over a loaded video.
- The full source frame is neutral; moving/resizing the crop handles creates a Crop action.
- Returning crop to the full frame removes the Crop action.
- Trim handles are always shown below the transport controls.
- The full duration is neutral; moving a trim handle creates a Trim action.
- Returning trim to the full duration removes the Trim action.
- QuickVideo uses its own play/pause, seek slider, and current/total time display instead of Gtk.MediaControls.

## Export test

The Export Video button now uses the desktop portal to choose an output path and runs FFmpeg asynchronously.
The generated FFmpeg command is printed in the terminal for debugging.

Currently wired to export:
- Trim
- Crop
- Rotate left/right
- Horizontal/vertical flip
- Brightness, contrast, saturation and gamma
- Volume, mute, normalization and audio removal

Join Clips is intentionally blocked during export for now rather than generating an incorrect concat command.
The first export target uses H.264 (`libx264`, CRF 18) and AAC 192 kb/s.

## Optional YouTube/video downloads

Install `yt-dlp`:

```bash
sudo apt install yt-dlp
```

## Debian package

Build the package locally with:

```bash
./packaging/build-deb.sh 0.1.0
```

The package is written to `dist/quickvideo_0.1.0_all.deb`.

Install it with:

```bash
sudo apt install ./dist/quickvideo_0.1.0_all.deb
```

After installation, launch QuickVideo from the application menu or with:

```bash
quickvideo
```

You can also open a video directly:

```bash
quickvideo /path/to/video.mp4
```

`yt-dlp` is a recommended dependency for the Download Video feature.

The application icon source is `data/icons/quickvideo.png`. Replace that PNG and rebuild the package to use your final artwork.
