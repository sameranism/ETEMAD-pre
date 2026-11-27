"""P14 deterministic, bounded image renditions. No network or publication."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Literal, Mapping

from backend.core.contracts import validate_payload
from backend.core.object_store import ObjectStore
from backend.modules.editorial.translator import canonical, digest

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
# Internal rendition profiles; these are not declarations of official API limits.
PROFILES = {"landscape": (1200, 675), "square": (1080, 1080), "portrait": (1080, 1920)}
_FORMATS = {"jpg": ("JPEG", "image/jpeg"), "png": ("PNG", "image/png"), "webp": ("WEBP", "image/webp")}


class ImageUnavailable(RuntimeError):
    """The declared image runtime dependency is unavailable."""


@dataclass(frozen=True)
class ImageRights:
    source_ref: str
    license_status: Literal["verified", "unknown", "restricted"] = "unknown"
    rights_ref: str | None = None
    ai_generated: bool = False

    def __post_init__(self) -> None:
        if not self.source_ref.strip() or len(self.source_ref) > 1024:
            raise ValueError("invalid image source reference")
        if self.license_status not in {"verified", "unknown", "restricted"}:
            raise ValueError("invalid image rights status")
        if self.rights_ref is not None and (not self.rights_ref.strip() or len(self.rights_ref) > 1024):
            raise ValueError("invalid rights reference")
        if self.license_status == "verified" and self.rights_ref is None:
            raise ValueError("verified rights require evidence")


@dataclass(frozen=True)
class PreparedImage:
    manifest: dict[str, Any]
    evidence: dict[str, Any]


def _store(objects: ObjectStore, data: bytes, extension: str, content_type: str) -> tuple[str, str]:
    checksum = hashlib.sha256(data).hexdigest()
    key = f"editorial/images/{checksum}.{extension}"
    prior = objects.get(key)
    if prior is not None and prior != data:
        raise ValueError("image object integrity conflict")
    if prior is None and objects.put(key, data, content_type=content_type) != key:
        raise ValueError("image object reference changed")
    if objects.get(key) != data:
        raise ValueError("image object verification failed")
    return key, checksum


def prepare_image(
    article: Mapping[str, Any], data: bytes, *, objects: ObjectStore, rights: ImageRights,
    alt_text: str, profiles: tuple[str, ...] = ("landscape", "square", "portrait"),
    output_format: str = "webp", fit: Literal["contain", "crop"] = "contain",
) -> PreparedImage:
    validate_payload("article", article)
    if (not 0 < len(data) <= MAX_IMAGE_BYTES or not alt_text.strip() or len(alt_text) > 1000
            or not profiles or len(profiles) > 3 or len(set(profiles)) != len(profiles)
            or any(profile not in PROFILES for profile in profiles)
            or output_format not in _FORMATS or fit not in {"contain", "crop"}):
        raise ValueError("invalid image request")
    if rights.license_status == "restricted":
        raise ValueError("restricted image cannot be prepared")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        raise ImageUnavailable("image runtime is unavailable") from None

    # Decode only permitted raster codecs, not SVG, archives, documents or animated media.
    try:
        with Image.open(BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as opened:
            width, height = opened.size
            if (not 0 < width <= 10_000 or not 0 < height <= 10_000
                    or width * height > MAX_PIXELS or getattr(opened, "n_frames", 1) != 1):
                raise ValueError("unsupported image dimensions or animation")
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            # Flatten alpha onto neutral white and omit all EXIF/ICC/XMP metadata.
            rgba = oriented.convert("RGBA")
            clean = Image.new("RGB", rgba.size, "white")
            clean.paste(rgba, mask=rgba.getchannel("A"))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("invalid raster image") from None

    source_hash = hashlib.sha256(data).hexdigest()
    assets: list[dict[str, Any]] = []
    evidence_assets: list[dict[str, Any]] = []
    label = ("تصویر تولیدشده با هوش مصنوعی (اعلام منبع) — " + alt_text
             if rights.ai_generated else alt_text)
    codec, mime = _FORMATS[output_format]
    for profile in profiles:
        size = PROFILES[profile]
        if fit == "crop":
            canvas = ImageOps.fit(clean, size, method=Image.Resampling.LANCZOS)
        else:
            contained = ImageOps.contain(clean, size, method=Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, "white")
            canvas.paste(contained, ((size[0] - contained.width)//2, (size[1] - contained.height)//2))
        output = BytesIO()
        if codec == "JPEG":
            canvas.save(output, format=codec, quality=90, optimize=False, progressive=False)
        elif codec == "WEBP":
            canvas.save(output, format=codec, quality=88, method=4)
        else:
            canvas.save(output, format=codec, compress_level=6)
        payload = output.getvalue()
        key, checksum = _store(objects, payload, output_format, mime)
        assets.append({"uri": key, "width": size[0], "height": size[1],
                       "format": output_format, "license_status": rights.license_status,
                       "ai_generated": rights.ai_generated, "alt_text": label})
        evidence_assets.append({"profile": profile, "sha256": checksum, "bytes": len(payload)})
    manifest = {"schema_version": "1.0", "article_id": article["article_id"], "assets": assets,
                "created_at": datetime.now(timezone.utc).isoformat()}
    validate_payload("image_manifest", manifest)
    evidence = {"source_ref": rights.source_ref, "rights_ref": rights.rights_ref,
                "license_status": rights.license_status, "source_sha256": source_hash,
                "source_dimensions": list(clean.size), "ai_generated_declared": rights.ai_generated,
                "origin_verified": False, "fit": fit, "engine": "Pillow",
                "assets": evidence_assets, "brand_overlay": False,
                "rights_evidence_present": rights.license_status == "verified" and rights.rights_ref is not None,
                "publication_eligible": False}
    return PreparedImage(manifest, evidence)


def image_identity(article_id: str, version: int, prepared: PreparedImage) -> str:
    return "IMG-" + digest(canonical([article_id, version, prepared.manifest["assets"], prepared.evidence]))[:32]


def verify_asset(objects: ObjectStore, asset: Mapping[str, Any], evidence: Mapping[str, Any]) -> tuple[bytes, str]:
    fmt = asset["format"]
    if fmt not in _FORMATS:
        raise ValueError("invalid rendition format")
    key = "editorial/images/" + str(evidence["sha256"]) + "." + fmt
    if key != asset["uri"]:
        raise ValueError("image reference mismatch")
    data = objects.get(key)
    if data is None or hashlib.sha256(data).hexdigest() != evidence["sha256"]:
        raise ValueError("image checksum mismatch")
    return data, _FORMATS[fmt][1]
