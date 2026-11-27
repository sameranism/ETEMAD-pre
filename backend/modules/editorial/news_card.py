"""Generate a clearly labelled local typographic news card, not a scene photo."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Mapping

from backend.core.contracts import validate_payload
from backend.core.object_store import ObjectStore
from backend.modules.editorial.image_processor import (
    PROFILES,
    ImageUnavailable,
    PreparedImage,
    _store,
)
from backend.modules.editorial.translator import digest


def prepare_news_card(article: Mapping[str, Any], *, objects: ObjectStore) -> PreparedImage:
    validate_payload("article", article)
    try:
        from PIL import Image, ImageDraw, ImageFont, features
        if not features.check_feature("raqm"):
            raise ImageUnavailable("Persian text shaping unavailable")
        # No downloaded fonts, third-party imagery, invented logo or scene.
        font = ImageFont.truetype("DejaVuSans.ttf", 52, layout_engine=ImageFont.Layout.RAQM)
        small = ImageFont.truetype("DejaVuSans.ttf", 28, layout_engine=ImageFont.Layout.RAQM)
    except (ImportError, OSError):
        raise ImageUnavailable("local Persian card renderer unavailable") from None
    title = " ".join(str(article["title"]).split())[:300]
    if not title:
        raise ValueError("news card requires a title")
    assets: list[dict[str, Any]] = []
    evidence_assets: list[dict[str, Any]] = []
    for profile, (width, height) in PROFILES.items():
        card = Image.new("RGB", (width, height), "#102a36")
        draw = ImageDraw.Draw(card)
        margin = 72
        draw.rectangle((0, 0, width, 16), fill="#1eaa99")
        draw.text((width-margin, 62), "کارت خبری | گرافیک تحریریه", font=small,
                  anchor="ra", direction="rtl", fill="#9ae7d8")
        lines: list[str] = []
        current = ""
        for word in title.split():
            candidate = (current + " " + word).strip()
            if draw.textlength(candidate, font=font, direction="rtl") <= width-2*margin:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
                while current and draw.textlength(current, font=font, direction="rtl") > width-2*margin:
                    count = len(current)-1
                    while count > 0 and draw.textlength(current[:count], font=font, direction="rtl") > width-2*margin:
                        count -= 1
                    if count == 0:
                        raise ValueError("headline cannot be rendered")
                    lines.append(current[:count])
                    current = current[count:]
        if current:
            lines.append(current)
        max_lines = min(9, (height-230)//77)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            last = lines[-1]
            while last and draw.textlength(last+"…", font=font, direction="rtl") > width-2*margin:
                last = last[:-1]
            lines[-1] = last+"…"
        y = max(150, (height-len(lines)*77)//2)
        for line in lines:
            draw.text((width-margin, y), line, font=font, anchor="ra", direction="rtl", fill="#ffffff")
            y += 77
        draw.text((width-margin, height-78), "تصویر واقعی رویداد نیست", font=small,
                  anchor="ra", direction="rtl", fill="#c6d6dc")
        buffer = BytesIO()
        card.save(buffer, format="WEBP", quality=88, method=4)
        payload = buffer.getvalue()
        key, checksum = _store(objects, payload, "webp", "image/webp")
        assets.append({"uri": key, "width": width, "height": height, "format": "webp",
                       "license_status": "unknown", "ai_generated": False,
                       "alt_text": "گرافیک خبری، نه تصویر واقعی: " + title})
        evidence_assets.append({"profile": profile, "sha256": checksum, "bytes": len(payload)})
    manifest = {"schema_version": "1.0", "article_id": article["article_id"], "assets": assets,
                "created_at": datetime.now(timezone.utc).isoformat()}
    validate_payload("image_manifest", manifest)
    return PreparedImage(manifest, {"source_ref": article["article_id"],
                         "rights_ref": None, "license_status": "unknown",
                         "source_sha256": digest(title), "origin_verified": True,
                         "ai_generated_declared": False, "engine": "Pillow",
                         "origin": "local_typographic_card", "template": "news-card-v1",
                         "assets": evidence_assets, "brand_overlay": False,
                         "rights_evidence_present": False, "publication_eligible": False})
