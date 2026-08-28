# QuickVideo

QuickVideo brings FFmpeg and yt-dlp together in a lightweight GTK4 app for quick video jobs. Without leaving the app, you can download a video from YouTube or Instagram, crop and trim it, adjust the picture or audio, and even censor parts you don't want to show with black bars, then convert and export it ready to share.

![QuickVideo screenshot](screenshot.png)

## Features

### Crop, Trim & Rotate

- Interactive crop rectangle directly over the video preview
- Drag handles for moving and resizing the crop
- Preset, free and custom aspect ratios
- Exact X, Y, width and height controls
- Interactive trim handles on the timeline
- Exact trim start/end times
- Rotate left or right
- Flip horizontally or vertically

### Image & Playback

Adjust:

- Brightness
- Contrast
- Saturation
- Gamma
- Temperature
- Sharpness
- Playback speed

Image adjustments can be previewed before exporting.

### Audio

Adjust:

- Volume
- Mute
- Normalize
- Remove audio
- Audio pitch
- Voice clarity
- Rumble reduction

### Censor Video

- Draw black censor bars directly on the video preview
- Move and resize censor regions
- Set individual start and end times
- Use multiple censor regions at once
- Color-coded regions and timeline ranges
- Censor bars are rendered into the exported video with FFmpeg

### Video Downloads

With `yt-dlp` installed, QuickVideo can download videos from YouTube, Instagram and other sites supported by `yt-dlp`, then open them directly for editing.

Downloaded videos are saved to:

```text
~/Downloads/QuickVideo/
```

### Export

QuickVideo builds the appropriate FFmpeg filter chain from your edits and exports the result without modifying the original file.

Export options include different quality/compression levels, lossless output and target file size.

## Ubuntu / Debian dependencies

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 ffmpeg \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav
```

For video downloading, optionally install:

```bash
sudo apt install yt-dlp
```

## Run from source

From the project directory:

```bash
python3 run.py
```

Or open a video directly:

```bash
python3 run.py /path/to/video.mp4
```

## Debian package

Build a package with:

```bash
./packaging/build-deb.sh 0.1.0
```

The resulting package is written to:

```text
dist/quickvideo_0.1.0_all.deb
```

Install it with:

```bash
sudo apt install ./dist/quickvideo_0.1.0_all.deb
```

QuickVideo can then be launched from the application menu or from a terminal:

```bash
quickvideo
```

You can also open a video directly:

```bash
quickvideo /path/to/video.mp4
```

`yt-dlp` is a recommended rather than required dependency.

## Status

QuickVideo is under active development. It is intended as a quick editing utility rather than a replacement for a full non-linear video editor.
