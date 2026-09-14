"""Images from Files, for the vision models — normalisation and variants.

ADDED 2026-09-13 (Files API design §4.4 image extractor and §5.5 "images from
files"). Two jobs, one Pillow codepath, no engine and no database:

* NORMALISE at processing time (`normalise`): the untrusted upload becomes a
  small set of clean, EXIF-stripped, metadata-free variants on disk. Every
  decode of hostile bytes is refused ABOVE the kind's pixel ceiling by an
  explicit width x height check made after the header is read and before a
  single pixel is decoded (`_refuse_over_ceiling`) — Pillow's own guard only
  errors at TWICE its limit, so it is a backstop, not the ceiling — and the
  animation formats are reduced to their first frame so a 10,000-frame GIF is
  one picture.

* SERVE at request time (`variant_for_detail` + `to_data_url`, and
  `resize_data_url` for an inline `image_url`): a `detail` maps to exactly one
  stored variant, which is read (a few MB) and handed to the engine as a
  `data:` URL. A remote URL is NEVER produced here — the engine would fetch it
  from inside the cluster network (SSRF, CONTRACT §8.1); only `data:` reaches
  an engine.

WHY THESE EDGES AND THESE TOKEN NUMBERS. Measured on the served model's
`/tokenize` endpoint (frontend/lib/images.ts, 2026-08-29): 1,013 tokens at
1280x800, 1,413 at 1600x900, 3,613 at 2560x1440, 8,173 at 3840x2160 — image
tokens grow with pixel count, so the long edge is the knob. `low` = 896 px
(~525 tokens, the video-frame size), `auto` = 1,600 px (~1,413), `high` /
`original` = 2,560 px (~3,613). Above 2,560 buys tokens, not legibility for a
model that resamples anyway, so nothing larger is ever stored or sent.

WHY IT IS NOT `engines/document.py` OR `video/screen._data_url`. Those decode
into a request's memory on the event loop and never resize; the chat path's own
gap. Here the resize is done once, off the loop (call `normalise` /
`resize_data_url` via `asyncio.to_thread`), and the bomb guard is explicit.
"""
from __future__ import annotations

import base64
import binascii
import io
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# storage's realpath fence — variants are only ever written under a derived dir
# the caller already validated, but writing through it keeps the invariant in
# one place. Imported lazily inside the writer so this module loads without the
# rest of the package present (the tests import it alone).


# --------------------------------------------------------------------------
# Settings, read the design's way: getattr(settings, <lower>, None), then the
# environment parsed with config.py's rules, then the default. limits.py owns
# the same two values for the extractor; kept here as a fallback so this module
# imports and runs even when limits.py is mid-edit in the shared worktree.
# --------------------------------------------------------------------------

_MIB = 1024 * 1024


def _setting_int(name: str, default: int) -> int:
    try:
        from ..config import settings  # noqa: PLC0415

        value = getattr(settings, name.lower(), None)
        if value is not None:
            return int(value)
    except Exception:  # noqa: BLE001 — settings is advisory here
        pass
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def image_max_pixels() -> int:
    """PUBLIC_API_FILES_IMAGE_MAX_PIXELS (89,478,485 = Pillow's default
    MAX_IMAGE_PIXELS). An image whose width x height EXCEEDS this is refused
    before decode by `_refuse_over_ceiling` — the hard ceiling is exactly this
    number, never the process default which other code may have moved, and
    never Pillow's 2x error threshold (see `_refuse_over_ceiling`)."""
    return max(1, _setting_int("PUBLIC_API_FILES_IMAGE_MAX_PIXELS", 89_478_485))


def image_max_bytes() -> int:
    """PUBLIC_API_FILES_IMAGE_MAX_BYTES (64 MiB): the cap on the original image
    the extractor decodes; the model sees ≤ 2,560 px regardless."""
    return max(1, _setting_int("PUBLIC_API_FILES_IMAGE_MAX_BYTES", 64 * _MIB))


# --------------------------------------------------------------------------
# The variant table (design §5.5).
# --------------------------------------------------------------------------

#: `detail` → the long edge in pixels of the variant to send.
VARIANT_EDGES: Dict[str, int] = {
    "low": 896,
    "auto": 1600,
    "high": 2560,
    "original": 2560,  # the largest STORED variant, never the raw upload
}

#: `detail` → the measured approximate token cost on the main model, for the
#: planner's image budget. Read from frontend/lib/images.ts's /tokenize run.
VARIANT_TOKENS: Dict[str, int] = {
    "low": 525,
    "auto": 1413,
    "high": 3613,
    "original": 3613,
}

#: The edges a variant file may be written at, small to large. `image.png` is
#: the canonical normalised copy at the largest of these that fits the source.
VARIANT_LADDER: Tuple[int, ...] = (896, 1600, 2560)

#: The default when a caller omits `detail`.
DEFAULT_DETAIL = "auto"

#: Formats whose first frame is the picture; the rest are animation we drop.
_MULTIFRAME_FORMATS = {"GIF", "TIFF", "WEBP", "APNG", "PNG"}

_DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|gif|webp|bmp|tiff));base64,", re.IGNORECASE)


@dataclass(frozen=True)
class Variant:
    """One written variant file."""

    name: str          # e.g. "image_896.jpg" or "image.png"
    edge: int          # the long edge it was written at
    path: str
    mime: str
    bytes: int


@dataclass(frozen=True)
class Normalised:
    """The result of `normalise`: facts for the File object plus what was
    written under `derived/`."""

    width: int         # of the source, after EXIF transpose
    height: int
    format: str        # the source format Pillow reported (PNG, JPEG, …)
    has_alpha: bool
    variants: Tuple[Variant, ...]

    def facts(self) -> Dict[str, Any]:
        return {"width": self.width, "height": self.height, "format": self.format}


class ImageError(ValueError):
    """The bytes could not be read as an image, or exceed a ceiling."""


class ImageTooLarge(ImageError):
    """The image's width x height exceeds the pixel ceiling (a bomb, or simply
    too big). Distinct from an undecodable image so `resize_data_url` refuses
    it instead of passing the caller's bytes on to the engine."""


def _refuse_over_ceiling(im: Any) -> None:
    """Refuse an opened image whose pixel count exceeds `image_max_pixels()`.

    WHY AN EXPLICIT CHECK (review finding, 2026-09-13). Pillow's guard is NOT
    a hard stop at MAX_IMAGE_PIXELS: it emits a DecompressionBombWarning above
    the value and raises DecompressionBombError only above TWICE it. Measured
    on the venv's Pillow 12.3.0 with MAX_IMAGE_PIXELS = 1,000,000: a 1,200 x
    1,200 (1.44 M px) PNG only warned and DECODED; a 1,500 x 1,500 (2.25 M px)
    one raised ("exceeds limit of 2000000 pixels"). So trusting Pillow alone
    let 178,956,970 px through a "89,478,485" ceiling — a few-KB solid PNG
    becoming a ~680 MiB RGBA allocation in the orchestrator process.

    `Image.open` reads only the header, so `im.size` is known here and nothing
    has been allocated for pixels yet. Call it after every open and after
    `seek(0)` (a multi-frame format may report a different size per frame)."""
    width, height = (int(v) for v in im.size)
    ceiling = image_max_pixels()
    if width * height > ceiling:
        raise ImageTooLarge(
            f"the image is {width}x{height} ({width * height} pixels); "
            f"the limit is {ceiling} pixels"
        )


# --------------------------------------------------------------------------
# Serve-time helpers (design §5.5). Pure and cheap; no Pillow needed.
# --------------------------------------------------------------------------


def normalise_detail(detail: Optional[str]) -> str:
    """A caller's `detail` → one of the table keys, defaulting to `auto`.

    Unknown values raise, so the route returns a 400 that names the field
    rather than silently sending the wrong resolution."""
    key = (detail or DEFAULT_DETAIL).strip().lower()
    if key not in VARIANT_EDGES:
        raise ImageError(
            f"detail must be one of {', '.join(sorted(VARIANT_EDGES))}; got {detail!r}"
        )
    return key


def variant_tokens(detail: Optional[str]) -> int:
    """The image-token cost the planner should charge for this `detail`."""
    return VARIANT_TOKENS[normalise_detail(detail)]


def variant_for_detail(derived_dir: str, detail: Optional[str]) -> Tuple[str, str]:
    """Pick the stored engine variant for `detail` → (path, mime).

    The chosen edge is the largest ladder variant that is ≤ the requested edge
    (`original` → the largest stored). A source smaller than the smallest
    requested edge yields the single small ladder variant. When no ladder
    variant was written (should not happen once processed) the canonical
    `image.png` is the fallback. Raises ImageError before processing."""
    want = VARIANT_EDGES[normalise_detail(detail)]
    ladder = _ladder_variants(derived_dir)
    if ladder:
        eligible = [v for v in ladder if v.edge <= want]
        chosen = max(eligible or ladder, key=lambda v: v.edge if v in eligible else -v.edge)
        return chosen.path, chosen.mime
    canon = os.path.join(derived_dir, "image.png")
    if os.path.exists(canon):
        return canon, "image/png"
    raise ImageError("no image variant has been produced for this file yet")


def _ladder_variants(derived_dir: str) -> List[Variant]:
    """Enumerate the `image_<edge>.{png,jpg}` engine variants (not the
    canonical `image.png` download copy, which §2.9 serves at route 8)."""
    out: List[Variant] = []
    try:
        names = os.listdir(derived_dir)
    except OSError:
        return out
    for name in names:
        m = re.fullmatch(r"image_(\d+)\.(png|jpg)", name)
        if m:
            path = os.path.join(derived_dir, name)
            out.append(Variant(name=name, edge=int(m.group(1)), path=path,
                               mime=_MIME_BY_EXT[m.group(2)], bytes=_size(path)))
    out.sort(key=lambda v: v.edge)
    return out


_MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def to_data_url(path: str) -> str:
    """Read a stored variant and encode it as a `data:` URL for the engine.

    The mime is taken from the extension, never guessed from the caller: these
    are files THIS module wrote, so the extension is trustworthy."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    mime = _MIME_BY_EXT.get(ext, "image/png")
    with open(path, "rb") as fh:
        raw = fh.read()
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------
# Normalise (design §4.4). Needs Pillow; call via asyncio.to_thread.
# --------------------------------------------------------------------------


def normalise(src_path: str, derived_dir: str) -> Normalised:
    """Decode `src_path`, strip metadata, and write the variant ladder.

    Order, exactly the design's (plus the explicit pixel ceiling after each
    open, see `_refuse_over_ceiling`):
      MAX_IMAGE_PIXELS = the kind's ceiling → open → verify() → reopen →
      exif_transpose → first frame only for multi-frame formats →
      convert to RGB (or RGBA when there is transparency) → variants at the
      ladder edges that are ≤ the source long edge, PNG for alpha else JPEG
      q=90, plus a canonical `image.png`/`image.jpg` at ≤ 2,560 px. No EXIF,
      ICC or other metadata is carried into any output.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: PLC0415

    limit = os.path.getsize(src_path) if os.path.exists(src_path) else 0
    if limit > image_max_bytes():
        raise ImageError(f"the image is {limit} bytes; the limit is {image_max_bytes()}")

    # Bomb guard, scoped to THIS decode: save and restore the process value so
    # we neither trust nor corrupt whatever else set it. Pillow WARNS above the
    # value and raises DecompressionBombError only above TWICE it (measured
    # 2026-09-13, Pillow 12.3.0), so the Pillow setting is a backstop and the
    # hard stop is `_refuse_over_ceiling`, called after every open.
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = image_max_pixels()
    try:
        # verify() consumes the file object, so it is a separate open from the
        # one we actually read pixels from (Pillow's documented contract).
        try:
            with Image.open(src_path) as probe:
                _refuse_over_ceiling(probe)
                probe.verify()
        except ImageError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise ImageError(f"the file could not be read as an image: {exc}") from exc
        except Image.DecompressionBombError as exc:
            raise ImageTooLarge(f"the image is too large to process: {exc}") from exc

        try:
            with Image.open(src_path) as im:
                _refuse_over_ceiling(im)
                src_format = (im.format or "").upper() or "UNKNOWN"
                if _is_multiframe(im):
                    im.seek(0)  # the first frame is the picture
                    _refuse_over_ceiling(im)
                im = ImageOps.exif_transpose(im)  # honour orientation, then drop it
                has_alpha = _has_alpha(im)
                base = im.convert("RGBA") if has_alpha else im.convert("RGB")
                base.load()
        except ImageError:
            raise
        except Image.DecompressionBombError as exc:
            raise ImageTooLarge(f"the image is too large to process: {exc}") from exc
        except (OSError, ValueError) as exc:
            raise ImageError(f"the image could not be decoded: {exc}") from exc

        width, height = base.width, base.height
        variants = _write_ladder(base, derived_dir, has_alpha=has_alpha)
        return Normalised(
            width=width, height=height, format=src_format,
            has_alpha=has_alpha, variants=tuple(variants),
        )
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def _is_multiframe(im: Any) -> bool:
    fmt = (getattr(im, "format", "") or "").upper()
    if fmt not in _MULTIFRAME_FORMATS:
        return False
    try:
        return int(getattr(im, "n_frames", 1)) > 1
    except Exception:  # noqa: BLE001 — a codec that cannot count frames is one frame
        return False


def _has_alpha(im: Any) -> bool:
    if im.mode in ("RGBA", "LA", "PA", "La"):
        return True
    return im.mode == "P" and "transparency" in im.info


def _write_ladder(base: Any, derived_dir: str, *, has_alpha: bool) -> List[Variant]:
    """Write each ladder edge ≤ the source long edge (JPEG q=90, or PNG when
    there is alpha) as the engine variants, plus the canonical `image.png`
    download copy (design §2.9: route 8's derived name for an image is exactly
    `image.png`, so the canonical copy is ALWAYS a lossless PNG ≤ 2,560 px,
    while the ladder variants sent to a vision model are the smaller JPEGs)."""
    _makedirs(derived_dir)
    long_edge = max(base.width, base.height)
    ext = "png" if has_alpha else "jpg"
    mime = _MIME_BY_EXT[ext]
    written: List[Variant] = []

    edges = [e for e in VARIANT_LADDER if e <= long_edge]
    if not edges:
        # The source is smaller than the smallest ladder edge: keep it at its
        # own size as the only rung, so a tiny logo is still served — never
        # upscaled.
        edges = [long_edge]

    for edge in edges:
        resized = _fit(base, edge)
        name = f"image_{edge}.{ext}"
        path = os.path.join(derived_dir, name)
        _save(resized, path, ext)
        written.append(Variant(name=name, edge=edge, path=path, mime=mime, bytes=_size(path)))

    largest_edge = max(v.edge for v in written)
    canon_path = os.path.join(derived_dir, "image.png")
    _save(_fit(base, largest_edge), canon_path, "png")
    written.append(Variant(name="image.png", edge=largest_edge, path=canon_path,
                           mime="image/png", bytes=_size(canon_path)))
    return written


def _fit(base: Any, edge: int) -> Any:
    """Downscale so the long edge is `edge`; never upscale."""
    from PIL import Image  # noqa: PLC0415

    long_edge = max(base.width, base.height)
    if long_edge <= edge:
        return base
    ratio = edge / long_edge
    size = (max(1, round(base.width * ratio)), max(1, round(base.height * ratio)))
    return base.resize(size, Image.LANCZOS)


def _save(im: Any, path: str, ext: str) -> None:
    """Write with NO metadata (no exif=, no icc_profile=)."""
    tmp = path + ".part"
    if ext == "png":
        im.save(tmp, format="PNG", optimize=True)
    else:
        im.save(tmp, format="JPEG", quality=90)
    os.replace(tmp, path)


def _makedirs(path: str) -> None:
    try:
        from . import storage  # noqa: PLC0415

        storage.makedirs(path)
    except Exception:  # noqa: BLE001 — storage may be mid-edit in the worktree
        os.makedirs(path, mode=0o750, exist_ok=True)


# --------------------------------------------------------------------------
# Inline `image_url` data URLs (design §5.5 last paragraph). The chat path
# never resized these; here we do, so a caller's 4K screenshot does not become
# 8,173 tokens. Call via asyncio.to_thread. Returns a data URL; never fetches.
# --------------------------------------------------------------------------


def resize_data_url(data_url: str, detail: Optional[str]) -> str:
    """Decode an inline base64 image, resize to `detail`, re-encode as a data
    URL. A remote URL is refused (SSRF); only `data:` in, only `data:` out.

    On any decode trouble the ORIGINAL data URL is returned unchanged rather
    than dropping the image — resizing is an optimisation, never a gate — but
    a non-`data:` input still raises, because sending it on would be the SSRF
    the whole rule exists to prevent, and an image over the pixel ceiling
    raises ImageTooLarge (refused before any pixel is decoded, and never passed
    on for the engine to decode instead)."""
    if not isinstance(data_url, str):
        raise ImageError("image_url must be a string data: URL")
    m = _DATA_URL_RE.match(data_url.strip())
    if m is None:
        if data_url.strip()[:5].lower() == "data:":
            raise ImageError("image_url must be a base64 data: URL of a PNG, JPEG, GIF, WebP, BMP or TIFF image")
        raise ImageError("image_url must be a data: URL; remote image URLs are not fetched")

    want_edge = VARIANT_EDGES[normalise_detail(detail)]
    payload = data_url.strip()[m.end():]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError("image_url does not hold valid base64 data") from None
    if len(raw) > image_max_bytes():
        raise ImageError(f"each image may be at most {image_max_bytes()} bytes")

    from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: PLC0415

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = image_max_pixels()
    try:
        with Image.open(io.BytesIO(raw)) as im:
            # The header is read; no pixel buffer exists yet. Refuse a bomb
            # HERE — Pillow's own error fires only at 2x its limit.
            _refuse_over_ceiling(im)
            if _is_multiframe(im):
                im.seek(0)
                _refuse_over_ceiling(im)
            im = ImageOps.exif_transpose(im)
            has_alpha = _has_alpha(im)
            base = (im.convert("RGBA") if has_alpha else im.convert("RGB"))
            base.load()
            if max(base.width, base.height) <= want_edge:
                # Already within budget: keep the caller's bytes rather than
                # re-encoding (and possibly enlarging a small PNG).
                return data_url
            resized = _fit(base, want_edge)
            buf = io.BytesIO()
            if has_alpha:
                resized.save(buf, format="PNG", optimize=True)
                out_mime = "image/png"
            else:
                resized.save(buf, format="JPEG", quality=90)
                out_mime = "image/jpeg"
            return f"data:{out_mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except ImageError:
        # ImageTooLarge from the explicit ceiling. ImageError IS a ValueError,
        # so without this clause the handler below would swallow the refusal
        # and hand the bomb's bytes on to the engine (mutation-checked in
        # test_an_inline_image_between_one_and_two_ceilings_is_refused).
        raise
    except Image.DecompressionBombError:
        # Pillow's backstop (2x its limit): a bomb we will not decode. Send
        # nothing on; refuse rather than resize.
        raise ImageTooLarge("the image is too large to process") from None
    except (UnidentifiedImageError, OSError, ValueError):
        # Undecodable here, but the engine may still accept it — this is only
        # an optimisation, so hand back exactly what the caller sent.
        return data_url
    finally:
        Image.MAX_IMAGE_PIXELS = previous


#: Every setting this module reads — for the integration team's config.py
#: block and the operator reference (Files design §8).
SETTINGS = (
    ("PUBLIC_API_FILES_IMAGE_MAX_PIXELS", "int", 89_478_485,
     "hard width x height ceiling for an API image decode (explicit check; "
     "Pillow's own error fires only at 2x)"),
    ("PUBLIC_API_FILES_IMAGE_MAX_BYTES", "int", 64 * _MIB,
     "largest original image the extractor will decode"),
)
