"""Profile photos are re-encoded on the server (2026-10).

The upload trusted the file extension and the client's Content-Type and stored
the bytes as sent, so a photo uploaded straight to the API kept its EXIF block —
GPS location and device serials included. `_clean_avatar` decodes the image and
writes a plain JPEG; anything that does not decode is refused.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api import server


def _jpeg_with_gps() -> bytes:
    im = Image.new("RGB", (64, 48), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"                      # Make
    exif[0x0110] = "Serial-0042"                    # Model (device identifier)
    from PIL.TiffImagePlugin import IFDRational
    exif.get_ifd(0x8825)[2] = (IFDRational(40, 1), IFDRational(26, 1), IFDRational(46, 1))  # GPS latitude
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_metadata_and_location_are_stripped():
    raw = _jpeg_with_gps()
    assert b"SecretCam" in raw
    clean = server._clean_avatar(raw)
    assert b"SecretCam" not in clean and b"Serial-0042" not in clean
    with Image.open(io.BytesIO(clean)) as im:
        assert im.format == "JPEG" and im.size == (64, 48)
        assert not im.getexif()


def test_large_photos_are_scaled_down():
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (0, 120, 200)).save(buf, "PNG")
    with Image.open(io.BytesIO(server._clean_avatar(buf.getvalue()))) as im:
        assert max(im.size) <= 1024


def test_a_file_that_is_not_an_image_is_refused():
    with pytest.raises(Exception):
        server._clean_avatar(b"<script>alert(1)</script>")


def test_the_route_refuses_a_renamed_non_image(monkeypatch):
    monkeypatch.setattr(server, "_require_user", lambda request: "local")
    c = TestClient(server.app)
    r = c.post("/api/profile/avatar", files={"file": ("me.png", b"not an image at all", "image/png")})
    assert r.status_code == 400
    r = c.post("/api/profile/avatar", files={"file": ("me.jpg", _jpeg_with_gps(), "image/jpeg")})
    assert r.status_code == 200
