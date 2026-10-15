#include "repaint_fw.h"

#include <string.h>

static uint32_t be32(const uint8_t *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static void put_be32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

uint32_t rp_crc32(const uint8_t *data, size_t len) {
    /* bitová varianta bez tabulky – PLTE má max. 772 B, rychlost nehraje roli */
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        c ^= data[i];
        for (int k = 0; k < 8; k++)
            c = (c & 1u) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
    }
    return c ^ 0xFFFFFFFFu;
}

/* ---------------------------------------------------------------- A */

rp_status_t rp_mtrl_parse(const uint8_t *d, size_t len, rp_mtrl_t *out) {
    if (len < 4 || d[0] != 1)
        return RP_ERR_BAD_MTRL;
    out->version = d[0];
    out->n_materials = d[1];
    size_t p = 2 + 3u * d[1];
    if (len < p + 2)
        return RP_ERR_BAD_MTRL;
    out->orig_colors = d + 2;
    out->n_entries = (uint16_t)(d[p] << 8 | d[p + 1]);
    if (out->n_entries == 0 || out->n_entries > RP_MAX_ENTRIES ||
        len < p + 2 + (size_t)RP_ENTRY_SIZE * out->n_entries)
        return RP_ERR_BAD_MTRL;
    out->entries = d + p + 2;
    return RP_OK;
}

void rp_plte_recolor(const rp_mtrl_t *m, const rp_rgb_t *pal, int n_pal, uint8_t *plte) {
    for (unsigned i = 0; i < m->n_entries; i++) {
        const uint8_t *e = m->entries + RP_ENTRY_SIZE * i;
        uint8_t m1 = e[0], m2 = e[1], w = e[2], D = e[3], S = e[4];
        if (m1 == RP_MAT_NONE || m1 >= n_pal || m2 >= n_pal)
            continue;
        plte[3 * i + 0] = rp_shade(rp_mix(pal[m1].r, pal[m2].r, w), D, S);
        plte[3 * i + 1] = rp_shade(rp_mix(pal[m1].g, pal[m2].g, w), D, S);
        plte[3 * i + 2] = rp_shade(rp_mix(pal[m1].b, pal[m2].b, w), D, S);
    }
}

static const uint8_t PNG_SIG[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'};

rp_status_t rp_png_recolor_inplace(uint8_t *png, size_t len, const rp_rgb_t *pal, int n_pal) {
    if (len < 8 || memcmp(png, PNG_SIG, 8) != 0)
        return RP_ERR_NOT_PNG;

    uint8_t *mtrl = NULL, *plte = NULL;
    uint32_t mtrl_len = 0, plte_len = 0;
    for (size_t p = 8; p + 12 <= len;) {
        uint32_t clen = be32(png + p);
        if (p + 12 + (size_t)clen > len)
            return RP_ERR_TRUNCATED;
        uint8_t *tag = png + p + 4;
        if (!memcmp(tag, "mtRL", 4)) {
            mtrl = tag + 4;
            mtrl_len = clen;
        } else if (!memcmp(tag, "PLTE", 4)) {
            plte = tag + 4;
            plte_len = clen;
        } else if (!memcmp(tag, "IDAT", 4)) {
            break; /* mtRL i PLTE musí být před IDAT */
        }
        p += 12 + clen;
    }
    if (!mtrl)
        return RP_ERR_NO_MTRL;
    if (!plte)
        return RP_ERR_NO_PLTE;

    rp_mtrl_t m;
    rp_status_t st = rp_mtrl_parse(mtrl, mtrl_len, &m);
    if (st != RP_OK)
        return st;
    if (plte_len != 3u * m.n_entries)
        return RP_ERR_MISMATCH;

    rp_plte_recolor(&m, pal, n_pal, plte);
    put_be32(plte + plte_len, rp_crc32(plte - 4, plte_len + 4)); /* CRC přes tag+data */
    return RP_OK;
}

/* ---------------------------------------------------------------- B */

static inline void gbuf_px(uint8_t *px, const rp_rgb_t *pal, int n_pal) {
    uint8_t D = px[0], S = px[1], m1 = px[2] >> 4, m2 = px[2] & 15;
    if (px[2] == RP_GBUF_BG || m1 >= n_pal || m2 >= n_pal) {
        px[0] = px[1] = px[2] = px[3] = 0;
        return;
    }
    if (m1 == m2) { /* čistý materiál, A = pokrytí – zůstává */
        px[0] = rp_shade(pal[m1].r, D, S);
        px[1] = rp_shade(pal[m1].g, D, S);
        px[2] = rp_shade(pal[m1].b, D, S);
    } else { /* hrana dvou materiálů, A = váha m1 */
        uint8_t w = px[3];
        px[0] = rp_shade(rp_mix(pal[m1].r, pal[m2].r, w), D, S);
        px[1] = rp_shade(rp_mix(pal[m1].g, pal[m2].g, w), D, S);
        px[2] = rp_shade(rp_mix(pal[m1].b, pal[m2].b, w), D, S);
        px[3] = 255;
    }
}

void rp_gbuffer_recolor(uint8_t *rgba, size_t n, const rp_rgb_t *pal, int n_pal) {
    for (size_t i = 0; i < n; i++)
        gbuf_px(rgba + 4 * i, pal, n_pal);
}

rp_status_t rp_qoi_gbuffer_decode(const uint8_t *q, size_t len, const rp_rgb_t *pal, int n_pal,
                                  uint8_t *out, size_t out_len, uint32_t *w, uint32_t *h) {
    if (len < 22 || memcmp(q, "qoif", 4) != 0)
        return RP_ERR_TRUNCATED;
    *w = be32(q + 4);
    *h = be32(q + 8);
    size_t n = (size_t)*w * *h;
    if (out_len < n * 4)
        return RP_ERR_TRUNCATED;

    uint8_t idx[64][4] = {{0}};
    uint8_t px[4] = {0, 0, 0, 255};
    size_t p = 14, o = 0;
    while (o < n) {
        if (p >= len)
            return RP_ERR_TRUNCATED;
        uint8_t b1 = q[p++];
        unsigned run = 1;
        if (b1 == 0xFE) {
            px[0] = q[p]; px[1] = q[p + 1]; px[2] = q[p + 2];
            p += 3;
        } else if (b1 == 0xFF) {
            memcpy(px, q + p, 4);
            p += 4;
        } else if ((b1 >> 6) == 0) {
            memcpy(px, idx[b1], 4);
        } else if ((b1 >> 6) == 1) {
            px[0] += ((b1 >> 4) & 3) - 2;
            px[1] += ((b1 >> 2) & 3) - 2;
            px[2] += (b1 & 3) - 2;
        } else if ((b1 >> 6) == 2) {
            uint8_t b2 = q[p++];
            int vg = (b1 & 63) - 32;
            px[0] += vg - 8 + (b2 >> 4);
            px[1] += vg;
            px[2] += vg - 8 + (b2 & 15);
        } else {
            run = (b1 & 63) + 1u;
        }
        memcpy(idx[(px[0] * 3 + px[1] * 5 + px[2] * 7 + px[3] * 11) % 64], px, 4);
        /* přebarvení až na výstupu – stav dekodéru (px, idx) musí zůstat G-buffer */
        for (; run && o < n; run--, o++) {
            memcpy(out + 4 * o, px, 4);
            gbuf_px(out + 4 * o, pal, n_pal);
        }
    }
    return RP_OK;
}
