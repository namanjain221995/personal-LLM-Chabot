"""ffmpeg input hardening for the API media lane (Files API design §6.4, §10).

The threat: a file that is really an HLS playlist or an ffconcat script makes
ffmpeg's demuxer open the LOCAL PATHS or URLs named inside it — reading another
tenant's audio, or probing an internal host. This proves the controls close it:

  * `SAFE_INPUT_ARGS` / `PIPE_INPUT_ARGS` are on every argv this module builds,
    and their format whitelist excludes hls, concat, image2, lavfi, sdp, rtsp
    and tee;
  * the argv canary refuses `-enable_drefs` / `-use_absolute_path` (whose
    ffmpeg defaults are false — asserted here on the real binary);
  * with REAL ffmpeg: an unhardened decode of a `.m3u8` naming a victim WAV
    reads the victim's exact bytes (the attack is real), while a decode with
    `SAFE_INPUT_ARGS`, and one through `pipe:0` with `PIPE_INPUT_ARGS`, both
    refuse it — and a playlist naming a FIFO is refused BEFORE the FIFO is
    opened, so a reader never blocks (the FIFO canary);
  * the progress relay names no engine, URL or path.

Pure-Python assertions always run. The real-ffmpeg reproductions resolve the
container's ffmpeg, or the static build the design verified on
(johnvansickle 7.0.2), and skip only if neither is present.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import pytest

from app.apifiles import media


# --- no database for this module ------------------------------------------
@pytest.fixture(scope="session")
def app_database():
    yield None


@pytest.fixture
def isolated_app_db():
    yield None


@pytest.fixture
def ambient_identity():
    yield None


def _resolve_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    static = "/home/techsphere/Downloads/saleforce-LLM/orchestrator/.venv/lib/python3.12/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-aarch64-v7.0.2"
    return static if os.path.exists(static) else None


def _run(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


# ======================================================================
# The constants and the argv canary (no ffmpeg needed)
# ======================================================================


def test_the_format_whitelist_excludes_every_path_opening_demuxer():
    for banned in ("hls", "concat", "image2", "lavfi", "sdp", "rtsp", "tee", "applehttp"):
        assert banned not in media.FORMAT_WHITELIST, f"{banned} must not be whitelisted"
    # And it still admits the real container/codec formats.
    for allowed in ("mov", "mp4", "matroska", "webm", "wav", "mp3", "flac", "aac"):
        assert allowed in media.FORMAT_WHITELIST


def test_safe_input_args_pin_the_protocol_and_format_whitelists():
    assert media.SAFE_INPUT_ARGS[:2] == ("-protocol_whitelist", "file,pipe")
    assert media.PIPE_INPUT_ARGS[:2] == ("-protocol_whitelist", "pipe")
    for args in (media.SAFE_INPUT_ARGS, media.PIPE_INPUT_ARGS):
        i = args.index("-format_whitelist")
        assert args[i + 1] == ",".join(media.FORMAT_WHITELIST)


def test_every_argv_this_module_builds_carries_the_whitelist_and_no_dangerous_flag():
    probe = media.probe_argv("/data/api-files/p/s/original")
    decode = media.audio_decode_pipe_argv("/data/video/abc/audio.wav.part")
    for argv in (probe, decode):
        joined = " ".join(argv)
        assert "-protocol_whitelist" in argv and "-format_whitelist" in argv
        assert ",".join(media.FORMAT_WHITELIST) in joined
        # The canary passes on the real argv...
        media.assert_no_dangerous_flags(argv)
        for danger in media.DANGEROUS_INPUT_FLAGS:
            assert danger not in argv
    # ...and the audio path uses pipe:0, not a file protocol, for its INPUT;
    # its output is a seekable `file:` path (never pipe:1, which buffers the
    # whole track in the orchestrator and leaves 0xFFFFFFFF header sizes).
    assert "pipe:0" in decode and media.PIPE_INPUT_ARGS[1] == "pipe"
    assert decode[-1] == "file:/data/video/abc/audio.wav.part"
    assert "pipe:1" not in decode


def test_the_argv_canary_refuses_a_dref_flag_however_it_is_spelled():
    for danger in ("-enable_drefs", "enable_drefs", "-use_absolute_path", "use_absolute_path"):
        with pytest.raises(media.UnsafeFfmpegArgv):
            media.assert_no_dangerous_flags(["ffmpeg", "-i", "x.mp4", danger, "true"])
    # A future edit that appended -enable_drefs to the probe argv would be
    # caught the same way (mutation check).
    with pytest.raises(media.UnsafeFfmpegArgv):
        media.assert_no_dangerous_flags([*media.probe_argv("/x"), "-enable_drefs", "true"])


def test_the_progress_relay_names_no_engine_url_or_path():
    ev = {"stage": "ocr", "status": "running", "percent": 50.0,
          "detail": "read by Unlimited-OCR at http://ocr:8000 from /data/video/abc/frames"}
    view = media.map_progress("video", ev)
    assert set(view) == {"stage", "step", "total_steps", "percent", "status"}
    blob = " ".join(str(v) for v in view.values())
    for leak in ("OCR", "http://", "/data/", "Unlimited"):
        assert leak not in blob


# ======================================================================
# Real ffmpeg: the attack is real, and the whitelist closes it
# ======================================================================


def test_ffmpeg_defaults_enable_drefs_and_use_absolute_path_to_false():
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg binary available")
    out = _run([ffmpeg, "-hide_banner", "-h", "demuxer=mov"])
    text = (out.stdout + out.stderr).decode("utf-8", "replace")
    assert "enable_drefs" in text and "use_absolute_path" in text
    for line in text.splitlines():
        if "enable_drefs" in line or "use_absolute_path" in line:
            assert "default false" in line, f"unexpected default: {line.strip()}"


def _make_victim_and_playlist(ffmpeg, tmp_path):
    victim = str(tmp_path / "victim.wav")
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
          "-f", "lavfi", "-i", "sine=frequency=880:duration=1", victim]).check_returncode()
    victim_sha = hashlib.sha256(open(victim, "rb").read()).hexdigest()
    playlist = str(tmp_path / "source.m3u8")
    with open(playlist, "w") as fh:
        fh.write(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\n{victim}\n#EXT-X-ENDLIST\n")
    return victim, victim_sha, playlist


def test_an_unhardened_decode_reads_the_victim_but_the_whitelist_refuses_it(tmp_path):
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg binary available")
    _victim, victim_sha, playlist = _make_victim_and_playlist(ffmpeg, tmp_path)

    # (a) UNHARDENED: the .m3u8 pulls in the victim; the copy equals it byte for
    #     byte — proof the attack is real.
    stolen = str(tmp_path / "stolen.wav")
    r = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              "-protocol_whitelist", "file,crypto,data", "-i", playlist, "-c", "copy", stolen])
    assert r.returncode == 0 and os.path.exists(stolen)
    assert hashlib.sha256(open(stolen, "rb").read()).hexdigest() == victim_sha

    # (b) HARDENED with SAFE_INPUT_ARGS: the hls demuxer is not on the format
    #     whitelist, so the file is refused and nothing is written.
    out_file = str(tmp_path / "hardened.wav")
    r2 = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               *media.SAFE_INPUT_ARGS, "-i", playlist, "-f", "wav", out_file])
    assert r2.returncode != 0
    assert b"whitelist" in r2.stderr.lower() or b"invalid data" in r2.stderr.lower()
    assert not os.path.exists(out_file) or os.path.getsize(out_file) == 0


def test_the_pipe_path_also_refuses_a_disguised_playlist(tmp_path):
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg binary available")
    _victim, _sha, playlist = _make_victim_and_playlist(ffmpeg, tmp_path)

    out_file = str(tmp_path / "piped.wav")
    with open(playlist, "rb") as fh:
        r = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                  *media.PIPE_INPUT_ARGS, "-i", "pipe:0", "-f", "wav", out_file],
                 stdin=fh)
    assert r.returncode != 0
    assert not os.path.exists(out_file) or os.path.getsize(out_file) == 0


def test_a_concat_script_is_refused(tmp_path):
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg binary available")
    victim = str(tmp_path / "v.wav")
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
          "-f", "lavfi", "-i", "sine=duration=1", victim]).check_returncode()
    concat = str(tmp_path / "evil.txt")
    with open(concat, "w") as fh:
        fh.write(f"ffconcat version 1.0\nfile {victim}\n")
    out_file = str(tmp_path / "o.wav")
    r = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              *media.SAFE_INPUT_ARGS, "-f", "concat", "-i", concat, "-f", "wav", out_file])
    assert r.returncode != 0  # concat is not on the format whitelist


def test_the_fifo_canary_refuses_a_playlist_before_opening_the_fifo(tmp_path):
    """A playlist naming a FIFO with no writer would block a reader forever;
    the whitelist refuses the demuxer BEFORE the FIFO is opened, so the ffmpeg
    process returns quickly instead of hanging."""
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg binary available")
    fifo = str(tmp_path / "thefifo")
    os.mkfifo(fifo)
    playlist = str(tmp_path / "fifo.m3u8")
    with open(playlist, "w") as fh:
        fh.write(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\n{fifo}\n#EXT-X-ENDLIST\n")

    # If the demuxer opened the FIFO, the read would block and this would
    # TimeoutExpired; a clean non-zero return means it was refused first.
    try:
        r = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                  *media.SAFE_INPUT_ARGS, "-i", playlist, "-f", "wav", str(tmp_path / "o.wav")],
                 stdin=subprocess.DEVNULL, timeout=8)
    except subprocess.TimeoutExpired:
        pytest.fail("ffmpeg blocked on the FIFO — the playlist was NOT refused before opening it")
    assert r.returncode != 0
