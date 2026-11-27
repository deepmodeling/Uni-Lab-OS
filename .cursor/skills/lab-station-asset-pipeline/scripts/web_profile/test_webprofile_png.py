"""web 档贴图处理的回归测试（PNG 无损重编码用例移植自设备包 tests/test_png_optimization.py，lanhq）。

    python -m pytest scripts/web_profile/test_webprofile_png.py
"""
import io
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import webprofile  # noqa: E402

Image = pytest.importorskip("PIL.Image")
PngImagePlugin = pytest.importorskip("PIL.PngImagePlugin")


def _png(image, **options):
    stream = io.BytesIO()
    image.save(stream, format="PNG", compress_level=0, **options)
    return stream.getvalue()


def _report():
    return {"tex_resized": 0, "tex_bytes_saved": 0}


@pytest.mark.parametrize("translucent", [False, True])
def test_small_images_keep_pixels_and_drop_redundant_channels(translucent):
    source = Image.new("RGBA", (13, 7), (90, 90, 90, 255))
    if translucent:
        source.putpixel((2, 3), (40, 40, 40, 0))
        source.putpixel((4, 5), (80, 80, 80, 128))
    raw = _png(source)
    encoded, action = webprofile.optimize_png(raw)
    with Image.open(io.BytesIO(encoded)) as output:
        assert output.size == source.size
        assert output.mode == ("LA" if translucent else "L")
        assert output.convert("RGBA").tobytes() == source.tobytes()
    assert action == "optimized" and len(encoded) < len(raw)


def test_color_key_transparency_survives():
    source = Image.new("RGB", (17, 9), (0, 0, 0))
    source.putpixel((3, 5), (34, 56, 78))
    raw = _png(source, transparency=(0, 0, 0))
    encoded, _ = webprofile.optimize_png(raw)
    with Image.open(io.BytesIO(raw)) as original, Image.open(io.BytesIO(encoded)) as output:
        assert output.convert("RGBA").tobytes() == original.convert("RGBA").tobytes()


def test_color_chunks_and_exif_are_preserved():
    info = PngImagePlugin.PngInfo()
    info.add(b"sRGB", b"\0")
    info.add(b"gAMA", struct.pack(">I", 45455))
    exif = Image.Exif()
    exif[274] = 6
    raw = _png(Image.new("RGB", (64, 32), (35, 80, 123)), pnginfo=info, exif=exif, dpi=(144, 144))
    encoded, _ = webprofile.optimize_png(raw)
    with Image.open(io.BytesIO(raw)) as original, Image.open(io.BytesIO(encoded)) as output:
        for key in ("srgb", "gamma", "dpi"):
            assert output.info[key] == original.info[key]
        assert output.getexif()[274] == 6


@pytest.mark.parametrize("kind,reason", [
    ("icc", "kept_icc_profile"),
    ("high_precision", "kept_high_precision"),
    ("hdr", "kept_hdr_metadata"),
    ("animation", "kept_animation"),
])
def test_unverified_types_return_exact_original(kind, reason):
    image = Image.new("RGB", (8, 8), (50, 80, 120))
    options = {}
    if kind == "icc":
        options["icc_profile"] = b"profile-needs-independent-color-validation"
    elif kind == "high_precision":
        image = Image.frombytes("I;16", (2, 2), struct.pack("<4H", 0, 1000, 32768, 65535))
    elif kind == "hdr":
        info = PngImagePlugin.PngInfo()
        info.add(b"cICP", bytes([9, 16, 0, 1]))
        options["pnginfo"] = info
    else:
        options.update(save_all=True, append_images=[Image.new("RGB", (8, 8), (90, 10, 30))], duration=100)
    raw = _png(image, **options)
    assert webprofile.optimize_png(raw) == (raw, reason)


def test_resize_textures_reencodes_small_png_and_resizes_large_ones(tmp_path):
    (tmp_path / "small.png").write_bytes(_png(Image.new("RGB", (64, 64), (30, 30, 30))))
    (tmp_path / "big.png").write_bytes(_png(Image.new("RGB", (2048, 1025), (23, 87, 149))))
    # 带 ICC 的大图不做重编码，但仍要缩到 1024（与原来的 web 档行为一致）。
    (tmp_path / "icc.png").write_bytes(_png(Image.new("RGB", (2048, 512), (1, 2, 3)), icc_profile=b"x" * 16))
    Image.new("RGB", (3000, 1500), (9, 9, 9)).save(tmp_path / "photo.jpg", quality=95)
    small_before = (tmp_path / "small.png").stat().st_size
    report = _report()
    webprofile.resize_textures(str(tmp_path), 1024, report)
    with Image.open(tmp_path / "big.png") as im:
        assert im.size == (1024, 513) and im.getpixel((0, 0)) == (23, 87, 149)
    with Image.open(tmp_path / "icc.png") as im:
        assert im.size == (1024, 256)
    with Image.open(tmp_path / "photo.jpg") as im:
        assert im.size == (1024, 512)
    assert (tmp_path / "small.png").stat().st_size < small_before
    assert report["tex_resized"] == 3
    assert report["png_actions"]["optimized"] == 2
    assert report["png_actions"]["kept_icc_profile"] == 1
    assert report["tex_bytes_saved"] > 0
