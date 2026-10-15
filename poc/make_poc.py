"""Vygeneruje POC výstupy do ../out a vypíše porovnání variant.

    python3 poc/make_poc.py
"""

import base64
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import repaint as rp

OUT = Path(__file__).resolve().parent.parent / "out"
OUT.mkdir(exist_ok=True)


def _font(size=13):
    # výchozí bitmapový font PIL neumí diakritiku
    for f in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()

# Testovací přemapování (index = původní slot T0..T4)
TESTS = {
    "identita": rp.ORIG_PALETTE,
    "prohozeni_slotu": [(0x2B, 0x2B, 0x2E), (0x2F, 0x6F, 0xD8), (0xF2, 0xC2, 0x30), (0xFA, 0x68, 0x31), (0x8A, 0x8C, 0x91)],
    "uplne_jine": [(0xC8, 0x1D, 0x25), (0x2E, 0x9E, 0x4F), (0x7A, 0x3F, 0xB5), (0xF0, 0xF0, 0xEC), (0x1A, 0x1A, 0x1C)],
    "sede_odstiny": [(0xEE, 0xEE, 0xEA), (0xFA, 0x68, 0x31), (0x2B, 0x2B, 0x2E), (0x2F, 0x6F, 0xD8), (0xFA, 0x68, 0x31)],
}


def b64_size(b: bytes) -> int:
    return len(base64.b64encode(b))


def main():
    t0 = time.time()
    gb = rp.render_gbuffer()
    print(f"render: {time.time() - t0:.1f}s")

    g = rp.downsample_gbuffer(gb)
    g_hard = rp.downsample_gbuffer(gb, mix=False)  # pro srovnání: bez míchání hran
    original = rp.render_truth(gb, rp.ORIG_PALETTE)  # dnešní náhled
    png_orig = rp.write_png_rgba(original)
    qoi_orig = rp.qoi_encode(original)

    png_a = rp.encode_variant_a(g)
    gbuf = rp.encode_variant_b(g)
    png_a_hard = rp.encode_variant_a(g_hard)
    gbuf_hard = rp.encode_variant_b(g_hard)
    png_b = rp.write_png_rgba(gbuf)
    qoi_b = rp.qoi_encode(gbuf)

    # sanity: QOI roundtrip a "identita" varianty A = původní PLTE bajt po bajtu
    assert np.array_equal(rp.qoi_decode(qoi_b), gbuf)
    assert rp.recolor_variant_a(png_a, rp.ORIG_PALETTE) == png_a

    (OUT / "original.png").write_bytes(png_orig)
    (OUT / "original.qoi").write_bytes(qoi_orig)
    (OUT / "variant_a_indexed.png").write_bytes(png_a)
    (OUT / "variant_b_gbuffer.png").write_bytes(png_b)
    (OUT / "variant_b_gbuffer.qoi").write_bytes(qoi_b)
    (OUT / "materials.json").write_text(
        json.dumps([{"name": n, "color": "#%02x%02x%02x" % c} for n, c in rp.MATERIALS], indent=2)
    )

    files = {
        "original.png (dnes)": png_orig,
        "original.qoi (dnes)": qoi_orig,
        "A: indexed.png + mtRL": png_a,
        "B: gbuffer.png": png_b,
        "B: gbuffer.qoi": qoi_b,
    }
    sizes = {k: len(v) for k, v in files.items()}
    print("\nVelikosti 440×240 (bajty / base64 v g-code):")
    for k, v in files.items():
        print(f"  {k:24s} {len(v):7d}  /  {b64_size(v):7d}")

    # --- porovnání proti ground truth ---
    metrics = {}
    rows = []
    for name, pal in TESTS.items():
        truth = rp.render_truth(gb, pal)
        res = {
            "baseline (bez masek)": rp.recolor_baseline(original, rp.ORIG_PALETTE, pal),
            "A bez míchání hran": rp.read_png_rgba(rp.recolor_variant_a(png_a_hard, pal)),
            "B bez míchání hran": rp.recolor_variant_b(gbuf_hard, pal),
            "A: indexed PNG": rp.read_png_rgba(rp.recolor_variant_a(png_a, pal)),
            "B: G-buffer": rp.recolor_variant_b(gbuf, pal),
        }
        metrics[name] = {}
        row = [("ground truth", truth, None)]
        for k, img in res.items():
            m, de = rp.delta_e(img, truth)
            metrics[name][k] = m
            row.append((k, img, m))
        rows.append((name, row))
        Image.fromarray(res["A: indexed PNG"]).save(OUT / f"recolor_{name}_A.png")

    # --- testovací vektory pro implementaci ve firmware (viz poc/firmware) ---
    tv = OUT / "testvectors"
    tv.mkdir(exist_ok=True)
    for name, pal in TESTS.items():
        (tv / f"{name}.pal").write_bytes(np.asarray(pal, np.uint8).tobytes())
        (tv / f"{name}_A.png").write_bytes(rp.recolor_variant_a(png_a, pal))
        (tv / f"{name}_B.rgba").write_bytes(rp.recolor_variant_b(gbuf, pal).tobytes())

    print("\nΔE76 vs. ground truth (průměr / 99. percentil / podíl pixelů s ΔE>5):")
    for name, mm in metrics.items():
        print(f"  {name}")
        for k, m in mm.items():
            print(f"    {k:22s} {m['mean']:5.2f} / {m['p99']:6.2f} / {m['gt5'] * 100:5.1f} %")
    (OUT / "metrics.json").write_text(json.dumps({"sizes": sizes, "delta_e": metrics}, indent=2))

    # --- přehledový obrázek ---
    w, h = original.shape[1], original.shape[0]
    pad, head = 10, 22
    sheet = Image.new("RGB", (pad + 4 * (w + pad), pad + len(rows) * (h + head + pad)), (18, 18, 20))
    dr = ImageDraw.Draw(sheet)
    rows_all = rows
    rows = [(n, [c for c in row if "bez míchání" not in c[0]]) for n, row in rows]
    for r, (name, row) in enumerate(rows):
        y = pad + r * (h + head + pad)
        for c, (label, img, m) in enumerate(row):
            x = pad + c * (w + pad)
            txt = f"{name} · {label}" + (f" · ΔE {m['mean']:.2f}" if m else "")
            dr.text((x, y + 4), txt, fill=(220, 220, 220), font=_font())
            sheet.paste(Image.fromarray(rp.composite(img).astype(np.uint8)), (x, y + head))
    sheet.save(OUT / "comparison.png")

    # --- detail hran: výřez věže, zvětšený 6× bez vyhlazení, mřížka 3×2 ---
    box, z, cols = (190, 80, 250, 120), 6, 3
    name, row = next(r for r in rows_all if r[0] == "uplne_jine")
    cw, ch = (box[2] - box[0]) * z, (box[3] - box[1]) * z
    nr = (len(row) + cols - 1) // cols
    det = Image.new("RGB", (pad + cols * (cw + pad), nr * (head + ch + pad)), (18, 18, 20))
    dr = ImageDraw.Draw(det)
    for i, (label, img, m) in enumerate(row):
        x, y = pad + (i % cols) * (cw + pad), (i // cols) * (head + ch + pad)
        crop = Image.fromarray(rp.composite(img).astype(np.uint8)).crop(box)
        det.paste(crop.resize((cw, ch), Image.NEAREST), (x, y + head))
        dr.text((x, y + 4), label + (f" · ΔE {m['mean']:.2f}" if m else ""), fill=(220, 220, 220), font=_font())
    det.save(OUT / "edges_detail.png")
    print(f"\nhotovo za {time.time() - t0:.1f}s → {OUT}")


if __name__ == "__main__":
    main()
