"""Actual P14 codec tests. Legacy contract-only CI does not install Pillow."""
from io import BytesIO

import pytest

from backend.core.object_store import LocalObjectStore
from backend.modules.editorial.image_processor import ImageRights, prepare_image, verify_asset

ARTICLE = {"schema_version": "1.0", "article_id": "ART-IMAGE-FIXTURE", "news_id": "NEWS-IMAGE",
           "stream": "domestic", "desk": "economy", "title": "Fixture", "lead": "Fixture",
           "body": "Fixture", "short_summary": "Fixture", "source_refs": ["NEWS-IMAGE"],
           "framework_version": "fixture:1", "editorial_rules_version": "fixture:1",
           "status": "draft", "created_at": "2026-11-27T00:00:00Z"}


@pytest.fixture
def raster():
    image = pytest.importorskip("PIL.Image", reason="codec dependency installed by primary backend CI")
    buffer = BytesIO()
    image.new("RGBA", (120, 60), (200, 30, 60, 128)).save(buffer, format="PNG")
    return image, buffer.getvalue()


def test_renditions_preserve_exact_dimensions_and_checksum(tmp_path, raster):
    image, raw = raster
    store = LocalObjectStore(tmp_path)
    rights = ImageRights("fixture:upload", "unknown", ai_generated=True)
    first = prepare_image(ARTICLE, raw, objects=store, rights=rights, alt_text="نمونه")
    second = prepare_image(ARTICLE, raw, objects=store, rights=rights, alt_text="نمونه")
    assert first.manifest["assets"] == second.manifest["assets"]
    assert first.evidence == second.evidence
    assert first.evidence["publication_eligible"] is False
    assert first.evidence["brand_overlay"] is False
    assert len(first.manifest["assets"]) == 3
    for asset, evidence in zip(first.manifest["assets"], first.evidence["assets"], strict=True):
        data, mime = verify_asset(store, asset, evidence)
        assert mime == "image/webp"
        with image.open(BytesIO(data)) as decoded:
            assert decoded.size == (asset["width"], asset["height"])
            assert decoded.mode == "RGB"
            assert not decoded.info.get("exif")
        assert asset["license_status"] == "unknown" and asset["ai_generated"]
        assert "هوش مصنوعی" in asset["alt_text"]


@pytest.mark.parametrize("fmt,mime", [("jpg", "image/jpeg"), ("png", "image/png")])
def test_crop_and_formats_are_real(tmp_path, raster, fmt, mime):
    image, raw = raster
    store = LocalObjectStore(tmp_path)
    result = prepare_image(ARTICLE, raw, objects=store,
                           rights=ImageRights("fixture:upload", "verified", "fixture:rights"),
                           alt_text="fixture", profiles=("square",), output_format=fmt, fit="crop")
    data, content_type = verify_asset(store, result.manifest["assets"][0], result.evidence["assets"][0])
    assert content_type == mime
    assert image.open(BytesIO(data)).size == (1080, 1080)
    assert result.evidence["rights_evidence_present"] is True
    assert result.evidence["publication_eligible"] is False


def test_rejects_bad_raster_restricted_rights_and_oversize(tmp_path, raster, monkeypatch):
    _, raw = raster
    store = LocalObjectStore(tmp_path)
    with pytest.raises(ValueError, match="require evidence"):
        ImageRights("fixture:upload", "verified")
    with pytest.raises(ValueError, match="restricted"):
        prepare_image(ARTICLE, raw, objects=store, rights=ImageRights("fixture:upload", "restricted"),
                      alt_text="fixture")
    with pytest.raises(ValueError, match="invalid raster"):
        prepare_image(ARTICLE, b"<svg/>", objects=store, rights=ImageRights("fixture:upload"),
                      alt_text="fixture")
    monkeypatch.setattr("backend.modules.editorial.image_processor.MAX_PIXELS", 100)
    with pytest.raises(ValueError, match="dimensions"):
        prepare_image(ARTICLE, raw, objects=store, rights=ImageRights("fixture:upload"),
                      alt_text="fixture")
