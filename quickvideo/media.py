import json
import subprocess
from pathlib import Path


def _rate(value):
    try:
        num, den = value.split('/', 1)
        den = float(den)
        if den == 0:
            return None
        return float(num) / den
    except (AttributeError, ValueError, ZeroDivisionError):
        return None


def _duration(seconds):
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return 'Unknown'

    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def _bitrate(value):
    try:
        bits = int(value)
    except (TypeError, ValueError):
        return None

    if bits >= 1_000_000:
        return f'{bits / 1_000_000:.1f} Mb/s'
    if bits >= 1_000:
        return f'{bits / 1_000:.0f} kb/s'
    return f'{bits} b/s'


def probe_video(path):
    path = Path(path)
    result = subprocess.run(
        [
            'ffprobe', '-v', 'error',
            '-show_format', '-show_streams',
            '-of', 'json',
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )

    data = json.loads(result.stdout)
    streams = data.get('streams', [])
    fmt = data.get('format', {})

    video = next((s for s in streams if s.get('codec_type') == 'video'), {})
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), {})

    fps = _rate(video.get('avg_frame_rate') or video.get('r_frame_rate'))
    bitrate = _bitrate(fmt.get('bit_rate') or video.get('bit_rate'))

    raw_duration = fmt.get('duration') or video.get('duration')
    try:
        duration_seconds = float(raw_duration)
    except (TypeError, ValueError):
        duration_seconds = None

    try:
        size_bytes = int(fmt.get('size') or path.stat().st_size)
    except (TypeError, ValueError, OSError):
        size_bytes = None

    if size_bytes is None:
        size = 'Unknown'
    elif size_bytes >= 1_000_000_000:
        size = f'{size_bytes / 1_000_000_000:.2f} GB'
    else:
        size = f'{size_bytes / 1_000_000:.1f} MB'

    channels = audio.get('channels')
    channel_layout = audio.get('channel_layout')
    if channel_layout:
        audio_layout = channel_layout
    elif channels:
        audio_layout = f'{channels} ch'
    else:
        audio_layout = None

    return {
        'duration': _duration(raw_duration),
        'duration_seconds': duration_seconds,
        'width': video.get('width'),
        'height': video.get('height'),
        'fps': fps,
        'video_codec': video.get('codec_name'),
        'pixel_format': video.get('pix_fmt'),
        'bitrate': bitrate,
        'audio_codec': audio.get('codec_name'),
        'audio_rate': audio.get('sample_rate'),
        'audio_layout': audio_layout,
        'size': size,
        'format': fmt.get('format_long_name') or fmt.get('format_name'),
    }
