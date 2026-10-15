"""Repaint POC – přebarvitelný náhledový obrázek pro vícebarevné tisky.

Modul obsahuje tři části:

1. "Slicer" – mini SDF raymarcher, který vyrenderuje vícemateriálový model
   do G-bufferu (id materiálu, difúzní stín D, odlesk S, pokrytí pixelu).
2. Kodéry dvou variant přebarvitelného náhledu:
     A) indexované PNG + privátní chunk `mtRL` (paleta = materiál × stín)
     B) "G-buffer" RGBA (R=stín, G=odlesk, B=materiál, A=pokrytí) jako PNG/QOI
3. "Firmware" – referenční přebarvení na straně tiskárny (jen celočíselná
   aritmetika, bez dekódování/kódování obrázku u varianty A).

Barevný model (shodný pro render i přebarvení):
    barva = albedo_materiálu * D + 255 * S
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

# --------------------------------------------------------------------------
# Scéna
# --------------------------------------------------------------------------

# Pět materiálů = pět slotů (T0..T4), např. MMU3 nebo XL s 5 hlavami.
MATERIALS = [
    ("T0 Black", (0x2B, 0x2B, 0x2E)),
    ("T1 Orange", (0xFA, 0x68, 0x31)),
    ("T2 White", (0xEE, 0xEE, 0xEA)),
    ("T3 Blue", (0x2F, 0x6F, 0xD8)),
    ("T4 Gray", (0x8A, 0x8C, 0x91)),
]
N_MAT = len(MATERIALS)
ORIG_PALETTE = np.array([c for _, c in MATERIALS], dtype=np.uint8)
BG = 255  # id "žádný materiál" (pozadí)


def _sd_round_box(p, b, r):
    q = np.abs(p) - (b - r)
    return np.linalg.norm(np.maximum(q, 0), axis=-1) + np.minimum(q.max(-1), 0) - r


def _sd_cyl(p, r, h):
    d = np.stack([np.hypot(p[..., 0], p[..., 1]) - r, np.abs(p[..., 2]) - h], -1)
    return np.minimum(d.max(-1), 0) + np.linalg.norm(np.maximum(d, 0), axis=-1)


def _sd_sphere(p, r):
    return np.linalg.norm(p, axis=-1) - r


def _scene(p):
    """Vrací (vzdálenost, id primitiva)."""
    base = _sd_round_box(p - (0, 0, 4), np.array([20.0, 20.0, 4.0]), 1.5)
    tower = _sd_cyl(p - (0, 0, 23), 11.0, 15.0)
    dome = _sd_sphere(p - (0, 0, 38), 11.0)
    knobs = np.minimum.reduce(
        [_sd_sphere(p - (sx * 14, sy * 14, 10), 4.0) for sx in (-1, 1) for sy in (-1, 1)]
    )
    d = np.stack([base, tower, dome, knobs], -1)
    return d.min(-1), d.argmin(-1)


def _material_of(p, prim):
    """Primitivum -> materiál. Věž má "MMU malované" pruhy (orange/white)."""
    mat = np.array([0, 1, 3, 4])[prim]
    band = ((p[:, 2] - 8.0) % 7.5) < 2.5
    mat[(prim == 1) & band] = 2
    return mat


def _normalize(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def render_gbuffer(w=440, h=240, ss=4):
    """Vyrenderuje scénu se supersamplingem ss×ss.

    Vrací dict polí tvaru (h, w, ss*ss): hit (bool), mat (int), D, S (float).
    """
    W, H = w * ss, h * ss
    target = np.array([0.0, 0.0, 21.0])
    az, el = np.radians(35), np.radians(28)
    eye_dir = np.array([np.sin(az) * np.cos(el), -np.cos(az) * np.cos(el), np.sin(el)])
    eye = target + 170.0 * eye_dir
    fwd = -eye_dir
    right = _normalize(np.cross(fwd, [0, 0, 1]))
    up = np.cross(right, fwd)
    half_h = np.tan(np.radians(21) / 2)
    half_w = half_h * W / H

    xs = ((np.arange(W) + 0.5) / W * 2 - 1) * half_w
    ys = (1 - (np.arange(H) + 0.5) / H * 2) * half_h
    gx, gy = np.meshgrid(xs, ys)
    rd = _normalize(fwd + gx[..., None] * right + gy[..., None] * up).reshape(-1, 3)
    n = rd.shape[0]

    t = np.full(n, 100.0)
    active = np.ones(n, bool)
    hit = np.zeros(n, bool)
    for _ in range(160):
        idx = np.flatnonzero(active)
        if idx.size == 0:
            break
        d, _ = _scene(eye + rd[idx] * t[idx, None])
        t[idx] += d
        done_hit = d < 0.004
        hit[idx[done_hit]] = True
        active[idx[done_hit | (t[idx] > 260)]] = False

    D = np.zeros(n)
    S = np.zeros(n)
    mat = np.full(n, BG)
    hi = np.flatnonzero(hit)
    p = eye + rd[hi] * t[hi, None]
    _, prim = _scene(p)
    e = 0.01
    nrm = np.stack(
        [
            _scene(p + off)[0] - _scene(p - off)[0]
            for off in (np.array([e, 0, 0]), np.array([0, e, 0]), np.array([0, 0, e]))
        ],
        -1,
    )
    nrm = _normalize(nrm)
    v = -rd[hi]
    L1 = _normalize(eye_dir + np.array([-0.5, 0.2, 0.9]))  # klíčové světlo
    L2 = _normalize(np.array([-1.0, 0.4, 0.1]))  # doplňkové
    n1 = np.maximum((nrm * L1).sum(-1), 0)
    n2 = np.maximum((nrm * L2).sum(-1), 0)
    D[hi] = np.clip(0.22 + 0.66 * n1 + 0.16 * n2, 0, 1)
    refl = 2 * n1[:, None] * nrm - L1
    S[hi] = 0.32 * np.maximum((refl * v).sum(-1), 0) ** 24
    mat[hi] = _material_of(p, prim)

    def blocks(a):
        return a.reshape(h, ss, w, ss).transpose(0, 2, 1, 3).reshape(h, w, ss * ss)

    return {"hit": blocks(hit), "mat": blocks(mat), "D": blocks(D), "S": blocks(S)}


def render_truth(gb, palette):
    """Ground truth: plný render s danou paletou, průměrovaný přes subsamply.

    Vrací RGBA uint8 (straight alpha) – takhle by vypadal náhled, kdyby ho
    slicer vyrenderoval rovnou s novými barvami.
    """
    pal = np.asarray(palette, float)
    hit = gb["hit"]
    m = np.where(hit, gb["mat"], 0)
    col = pal[m] * gb["D"][..., None] + 255 * gb["S"][..., None]
    col = np.minimum(col, 255) * hit[..., None]
    nh = hit.sum(-1)
    rgb = col.sum(2) / np.maximum(nh, 1)[..., None]
    a = nh / hit.shape[-1] * 255
    return np.dstack([rgb, a]).round().clip(0, 255).astype(np.uint8)


def downsample_gbuffer(gb, mix=True):
    """G-buffer na výsledné rozlišení.

    Každý pixel nese až dva materiály: převažující `m1`, druhý `m2` a váhu `w`
    (podíl m1 mezi nimi, 0.5..1). Díky tomu jsou hrany mezi materiály vyhlazené
    i po přebarvení – barvy se míchají až v tiskárně. `alpha` je pokrytí pixelu
    modelem (vyhlazení vůči pozadí).

    mix=False = původní chování (jen převažující materiál, schodovité hrany).
    """
    hit, mat = gb["hit"], gb["mat"]
    counts = np.stack([(hit & (mat == k)).sum(-1) for k in range(N_MAT)], -1)
    order = np.argsort(-counts, axis=-1, kind="stable")
    m1, m2 = order[..., 0], order[..., 1]
    c1 = np.take_along_axis(counts, m1[..., None], -1)[..., 0]
    c2 = np.take_along_axis(counts, m2[..., None], -1)[..., 0]
    nh = hit.sum(-1)
    if mix:
        sel = hit  # stín průměrujeme přes oba materiály
        m2 = np.where(c2 > 0, m2, m1)
        w = c1 / np.maximum(c1 + c2, 1)
    else:
        sel = hit & (mat == m1[..., None])
        m2 = m1
        w = np.ones(m1.shape)
    ns = np.maximum(sel.sum(-1), 1)
    D = (gb["D"] * sel).sum(-1) / ns
    S = (gb["S"] * sel).sum(-1) / ns
    alpha = nh / hit.shape[-1]
    m1 = np.where(nh > 0, m1, BG)
    m2 = np.where(nh > 0, m2, BG)
    return {"m1": m1, "m2": m2, "w": w, "D": D, "S": S, "alpha": alpha}


# --------------------------------------------------------------------------
# PNG utility (vlastní zápis kvůli řazení chunků a indexovaným barvám)
# --------------------------------------------------------------------------

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def png_chunks(png: bytes):
    assert png[:8] == PNG_SIG
    pos = 8
    while pos < len(png):
        (ln,) = struct.unpack(">I", png[pos : pos + 4])
        tag = png[pos + 4 : pos + 8]
        yield tag, png[pos + 8 : pos + 8 + ln]
        pos += 12 + ln


def write_png_indexed(idx: np.ndarray, plte: np.ndarray, trns: np.ndarray, extra=()) -> bytes:
    h, w = idx.shape
    raw = b"".join(b"\x00" + row.tobytes() for row in idx.astype(np.uint8))
    out = PNG_SIG + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0))
    for tag, data in extra:  # privátní chunky před PLTE – streamovací dekodér je zná dřív
        out += png_chunk(tag, data)
    out += png_chunk(b"PLTE", plte.astype(np.uint8).tobytes())
    out += png_chunk(b"tRNS", trns.astype(np.uint8).tobytes())
    out += png_chunk(b"IDAT", zlib.compress(raw, 9))
    return out + png_chunk(b"IEND", b"")


def write_png_rgba(rgba: np.ndarray) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def read_png_rgba(png: bytes) -> np.ndarray:
    import io

    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))


# --------------------------------------------------------------------------
# QOI (https://qoiformat.org) – formát, který používá Prusa firmware
# --------------------------------------------------------------------------


def qoi_encode(rgba: np.ndarray) -> bytes:
    h, w, _ = rgba.shape
    out = bytearray(b"qoif" + struct.pack(">IIBB", w, h, 4, 0))
    index = [(0, 0, 0, 0)] * 64
    prev = (0, 0, 0, 255)
    run = 0

    def s8(x):
        return ((x + 128) & 255) - 128

    for p in map(tuple, rgba.reshape(-1, 4).tolist()):
        if p == prev:
            run += 1
            if run == 62:
                out.append(0xC0 | (run - 1))
                run = 0
            continue
        if run:
            out.append(0xC0 | (run - 1))
            run = 0
        hsh = (p[0] * 3 + p[1] * 5 + p[2] * 7 + p[3] * 11) % 64
        if index[hsh] == p:
            out.append(hsh)
        else:
            index[hsh] = p
            if p[3] == prev[3]:
                vr, vg, vb = s8(p[0] - prev[0]), s8(p[1] - prev[1]), s8(p[2] - prev[2])
                vgr, vgb = vr - vg, vb - vg
                if -2 <= vr <= 1 and -2 <= vg <= 1 and -2 <= vb <= 1:
                    out.append(0x40 | (vr + 2) << 4 | (vg + 2) << 2 | (vb + 2))
                elif -32 <= vg <= 31 and -8 <= vgr <= 7 and -8 <= vgb <= 7:
                    out += bytes([0x80 | (vg + 32), (vgr + 8) << 4 | (vgb + 8)])
                else:
                    out += bytes([0xFE, p[0], p[1], p[2]])
            else:
                out += bytes([0xFF, *p])
        prev = p
    if run:
        out.append(0xC0 | (run - 1))
    return bytes(out + b"\x00" * 7 + b"\x01")


def qoi_decode(data: bytes) -> np.ndarray:
    assert data[:4] == b"qoif"
    w, h = struct.unpack(">II", data[4:12])
    n = w * h
    px = (0, 0, 0, 255)
    index = [(0, 0, 0, 0)] * 64
    out = []
    pos = 14
    while len(out) < n:
        b1 = data[pos]
        pos += 1
        if b1 == 0xFE:
            px = (data[pos], data[pos + 1], data[pos + 2], px[3])
            pos += 3
        elif b1 == 0xFF:
            px = tuple(data[pos : pos + 4])
            pos += 4
        elif b1 >> 6 == 0:
            px = index[b1]
        elif b1 >> 6 == 1:
            px = (
                (px[0] + ((b1 >> 4) & 3) - 2) & 255,
                (px[1] + ((b1 >> 2) & 3) - 2) & 255,
                (px[2] + (b1 & 3) - 2) & 255,
                px[3],
            )
        elif b1 >> 6 == 2:
            b2 = data[pos]
            pos += 1
            vg = (b1 & 63) - 32
            px = (
                (px[0] + vg - 8 + (b2 >> 4)) & 255,
                (px[1] + vg) & 255,
                (px[2] + vg - 8 + (b2 & 15)) & 255,
                px[3],
            )
        else:
            out.extend([px] * ((b1 & 63) + 1))
            continue
        index[(px[0] * 3 + px[1] * 5 + px[2] * 7 + px[3] * 11) % 64] = px
        out.append(px)
    return np.array(out, np.uint8).reshape(h, w, 4)


# --------------------------------------------------------------------------
# Varianta A: indexované PNG + chunk mtRL
# --------------------------------------------------------------------------
#
# mtRL v1 (big endian):
#   u8  version = 1
#   u8  n_materials
#   n × (u8 r, u8 g, u8 b)        původní barva materiálu (informativní)
#   u16 n_entries                 = počet položek PLTE
#   n_entries × (u8 m1, u8 m2, u8 w, u8 D, u8 S)
#        m1, m2 = index materiálu/nástroje (m1 = 255: položky se nedotýkat)
#        w      = váha m1 ve směsi 0..255 (255 a m1 == m2: čistý materiál)
#        D      = difúzní stín 0..255, S = odlesk 0..255
#
# Firmware:  base = (pal[m1]*w + pal[m2]*(255-w) + 127) / 255
#            PLTE[i] = min(255, (base*D + 127) / 255 + S)
# Ostatní dekodéry chunk ignorují (ancilární, privátní) a ukáží původní barvy.

MTRL = b"mtRL"
ENTRY = 5  # bajtů na položku


def _kmeans(X, k, iters=30, seed=0):
    uniq = np.unique(X, axis=0)
    if len(uniq) <= k:
        C = uniq
    else:
        rng = np.random.default_rng(seed)
        C = [X[rng.integers(len(X))]]
        d2 = ((X - C[0]) ** 2).sum(1)
        for _ in range(1, k):
            C.append(X[rng.choice(len(X), p=d2 / d2.sum())])
            d2 = np.minimum(d2, ((X - C[-1]) ** 2).sum(1))
        C = np.array(C)
        for _ in range(iters):
            lab = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(1)
            for j in range(len(C)):
                sel = lab == j
                if sel.any():
                    C[j] = X[sel].mean(0)
    lab = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(1)
    return C, lab


def mix_shade(pal, m1, m2, w, D, S):
    """Barevný model se směsí dvou materiálů (celočíselně, shodně s firmware).
    Pro m1 == m2, w == 255 je base == pal[m1] přesně."""
    pal = np.asarray(pal, np.int32)
    w = np.asarray(w, np.int32)[..., None]
    base = (pal[m1] * w + pal[m2] * (255 - w) + 127) // 255
    D = np.asarray(D, np.int32)[..., None]
    S = np.asarray(S, np.int32)[..., None]
    return np.minimum(255, (base * D + 127) // 255 + S)


def plte_from_mtrl(entries: np.ndarray, palette, orig_plte: np.ndarray) -> np.ndarray:
    """To, co dělá firmware: z (m1, m2, w, D, S) a nových barev spočítá PLTE."""
    out = orig_plte.astype(np.int32).copy()
    e = entries.astype(np.int32)
    n_pal = len(palette)
    ok = (e[:, 0] != 255) & (e[:, 0] < n_pal) & (e[:, 1] < n_pal)
    e = e[ok]
    out[ok] = mix_shade(palette, e[:, 0], e[:, 1], e[:, 2], e[:, 3], e[:, 4])
    return out.astype(np.uint8)


def encode_variant_a(g, palette=ORIG_PALETTE, budget=255, min_pair_px=12, pair_boost=2.0):
    """Kvantizace do ≤ 256 barev. Pixely se rozdělí do skupin podle materiálu
    (čisté) nebo dvojice materiálů (hrany); v každé skupině k-means v prostoru
    (w·D, (1-w)·D, S, alpha). Rozpočet barev ~ sqrt(počtu pixelů skupiny),
    dvojice mají váhu `pair_boost` (chyba na hraně je vidět víc)."""
    m1, m2, w, D, S, alpha = (g[k] for k in ("m1", "m2", "w", "D", "S", "alpha"))
    h, wd = m1.shape
    cov = alpha > 0
    lo, hi = np.minimum(m1, m2), np.maximum(m1, m2)
    wlo = np.where(m1 == lo, w, 1 - w)  # váha materiálu s nižším indexem
    keys = lo * 256 + hi
    # vzácné dvojice (pár pixelů v rohu) nestojí za vlastní barvy -> převažující materiál
    uk, uc = np.unique(keys[cov], return_counts=True)
    for k_, c_ in zip(uk, uc):
        if k_ // 256 != k_ % 256 and c_ < min_pair_px:
            sel = cov & (keys == k_)
            lo[sel] = hi[sel] = m1[sel]
            wlo[sel] = 1.0
    keys = lo * 256 + hi
    uk, uc = np.unique(keys[cov], return_counts=True)
    single = uk // 256 == uk % 256
    wts = np.sqrt(uc) * np.where(single, 1.0, pair_boost)  # hrany jsou na chybu citlivější
    k_per = np.maximum(np.where(single, 6, 3), np.floor(budget * wts / wts.sum())).astype(int)
    while k_per.sum() > budget:
        k_per[k_per.argmax()] -= 1

    idx = np.zeros((h, wd), np.uint8)
    entries = [(255, 255, 255, 0, 0, 0)]  # 0 = průhledné pozadí
    for key, k, is_single in zip(uk, k_per, single):
        a, b = int(key // 256), int(key % 256)
        sel = cov & (keys == key)
        # Barva je lineární v (w·D, (1-w)·D, S): out = p_a·x1 + p_b·x2 + S.
        # V těchto souřadnicích odpovídá euklidovská vzdálenost chybě barvy.
        x1 = wlo[sel] * D[sel]
        x2 = (1 - wlo[sel]) * D[sel]
        X = np.stack([x1, x2, S[sel], alpha[sel] * 0.6], -1)
        C, lab = _kmeans(X, k)
        base = len(entries)
        for c in C:
            dd = c[0] + c[1]
            ww = 255 if is_single else round(np.clip(c[0] / max(dd, 1e-6), 0, 1) * 255)
            entries.append((a, b, ww, round(dd * 255), round(c[2] * 255), round(min(c[3] / 0.6, 1) * 255)))
        idx[sel] = base + lab
    ent = np.array(entries, np.int64)
    n = len(ent)
    mtrl_ent = ent[:, :ENTRY].astype(np.uint8)
    plte = plte_from_mtrl(mtrl_ent, palette, np.zeros((n, 3), np.uint8))
    trns = ent[:, ENTRY].astype(np.uint8)

    payload = struct.pack(">BB", 1, N_MAT) + np.asarray(palette, np.uint8).tobytes()
    payload += struct.pack(">H", n) + mtrl_ent.tobytes()
    return write_png_indexed(idx, plte, trns, extra=[(MTRL, payload)])


def parse_mtrl(png: bytes):
    mt = next(d for t, d in png_chunks(png) if t == MTRL)
    p = 2 + 3 * mt[1]
    (n,) = struct.unpack(">H", mt[p : p + 2])
    return np.frombuffer(mt[p + 2 : p + 2 + ENTRY * n], np.uint8).reshape(n, ENTRY)


def recolor_variant_a(png: bytes, new_palette) -> bytes:
    """Firmware strana: přepíše jen chunk PLTE. IDAT se vůbec nedotkne."""
    chunks = list(png_chunks(png))
    entries = parse_mtrl(png)
    orig = np.frombuffer(next(d for t, d in chunks if t == b"PLTE"), np.uint8).reshape(-1, 3)
    plte = plte_from_mtrl(entries, new_palette, orig)
    out = PNG_SIG
    for t, d in chunks:
        out += png_chunk(t, plte.tobytes() if t == b"PLTE" else d)
    return out


# --------------------------------------------------------------------------
# Varianta B: G-buffer RGBA
# --------------------------------------------------------------------------
#
#   R = D, G = S
#   B = m1 << 4 | m2   (max. 15 materiálů; 0xFF = pozadí)
#   A = m1 == m2:  pokrytí pixelu (vyhlazení vůči pozadí)
#       m1 != m2:  váha m1 ve směsi (pixel je plně pokrytý)
#
# Pixel tak nese buď hranu materiál/materiál, nebo hranu model/pozadí. V místě,
# kde se potkají obě (vzácné), se uloží ta, jejíž zanedbání by bylo vidět víc.

B_BG = 0xFF


def encode_variant_b(g) -> np.ndarray:
    assert N_MAT <= 15, "varianta B nese index materiálu ve 4 bitech"
    m1, m2, w, D, S, alpha = (g[k] for k in ("m1", "m2", "w", "D", "S", "alpha"))
    cov = alpha > 0
    mix = cov & (m2 != m1) & ((1 - w) > (1 - alpha))
    r = np.where(cov, np.round(D * 255), 0)
    gg = np.where(cov, np.round(S * 255), 0)
    b = np.where(cov, (m1 << 4) | np.where(mix, m2, m1), B_BG)
    a = np.where(mix, np.round(w * 255), np.round(alpha * 255))
    return np.dstack([r, gg, b, a]).astype(np.uint8)


def recolor_variant_b(gbuf: np.ndarray, new_palette) -> np.ndarray:
    """Firmware strana: per-pixel LUT, u hranových pixelů navíc mix dvou barev."""
    n_pal = len(new_palette)
    b = gbuf[..., 2].astype(np.int32)
    m1, m2 = b >> 4, b & 15
    A = gbuf[..., 3].astype(np.int32)
    valid = (b != B_BG) & (m1 < n_pal) & (m2 < n_pal)
    mix = m1 != m2
    pal = np.zeros((16, 3), np.int32)
    pal[:n_pal] = np.asarray(new_palette, np.int32)
    w = np.where(mix, A, 255)
    rgb = mix_shade(pal, m1, m2, w, gbuf[..., 0], gbuf[..., 1])
    a = np.where(mix, 255, A)
    out = np.dstack([rgb, a]).astype(np.uint8)
    out[~valid] = 0
    return out


# --------------------------------------------------------------------------
# Baseline: přebarvení běžného RGB náhledu bez masek
# --------------------------------------------------------------------------


def recolor_baseline(rgba: np.ndarray, orig_palette, new_palette, lam=3.0) -> np.ndarray:
    """Pro každý pixel zkusí každý původní materiál m napasovat jako
    c ≈ o_m*d + s (d∈[0,1], s≥0) a vybere ten s nejmenším reziduem.
    Používá jen to, co už dnes v g-code je (barvy filamentů)."""
    c = rgba[..., :3].astype(float)
    best_r = np.full(c.shape[:2], np.inf)
    out = np.zeros_like(c)
    for o, nw in zip(np.asarray(orig_palette, float), np.asarray(new_palette, float)):
        oo, so, sc, oc = o @ o, o.sum(), c.sum(-1), c @ o
        det = oo * (3 + lam) - so * so
        d = ((3 + lam) * oc - so * sc) / det
        d = np.clip(d, 0, 1)
        s = np.clip((sc - d * so) / (3 + lam), 0, 255)
        res = ((c - (o * d[..., None] + s[..., None])) ** 2).sum(-1)
        better = res < best_r
        best_r[better] = res[better]
        out[better] = (nw * d[..., None] + s[..., None])[better]
    return np.dstack([np.clip(out, 0, 255), rgba[..., 3]]).round().astype(np.uint8)


# --------------------------------------------------------------------------
# Metriky
# --------------------------------------------------------------------------


def _srgb_to_lab(rgb):
    c = rgb / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array(
        [[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]
    )
    xyz = c @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack(
        [116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1
    )


def composite(rgba, bg=(30, 30, 32)):
    a = rgba[..., 3:4] / 255.0
    return rgba[..., :3] * a + np.array(bg) * (1 - a)


def delta_e(img, ref):
    """ΔE76 přes pixely, kde je model (alpha ref > 0), po složení na pozadí."""
    mask = ref[..., 3] > 0
    de = np.linalg.norm(_srgb_to_lab(composite(img)) - _srgb_to_lab(composite(ref)), axis=-1)
    v = de[mask]
    return {"mean": float(v.mean()), "p99": float(np.percentile(v, 99)), "gt5": float((v > 5).mean())}, de
