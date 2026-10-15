/*
 * Repaint – referenční implementace přebarvení náhledu na straně tiskárny.
 *
 * Čisté C99, bez alokací, bez float. Viz docs/SPEC.md a docs/FIRMWARE.md.
 */
#ifndef REPAINT_FW_H
#define REPAINT_FW_H

#include <stddef.h>
#include <stdint.h>

#define RP_MAT_NONE 255       /* položka/pixel nepatří žádnému materiálu */
#define RP_MAX_ENTRIES 256
#define RP_ENTRY_SIZE 5       /* mtRL: (m1, m2, w, D, S) */
#define RP_GBUF_BG 0xFF       /* varianta B: pozadí v kanálu B */

typedef struct {
    uint8_t r, g, b;
} rp_rgb_t;

typedef enum {
    RP_OK = 0,
    RP_ERR_NOT_PNG,
    RP_ERR_NO_MTRL,     /* PNG nemá chunk mtRL -> nepřebarvitelný, zobraz jak je */
    RP_ERR_NO_PLTE,
    RP_ERR_BAD_MTRL,    /* poškozený / neznámá verze -> zobraz jak je */
    RP_ERR_MISMATCH,    /* n_entries != počet položek PLTE */
    RP_ERR_TRUNCATED,
} rp_status_t;

/* Rozparsovaný chunk mtRL. Ukazatele míří do bufferu s PNG (žádná kopie). */
typedef struct {
    uint8_t version;
    uint8_t n_materials;
    const uint8_t *orig_colors; /* n_materials × RGB */
    uint16_t n_entries;
    const uint8_t *entries;     /* n_entries × (m1, m2, w, D, S) */
} rp_mtrl_t;

/* Jeden kanál: min(255, (c*D + 127)/255 + S). Sdílené variantou A i B. */
static inline uint8_t rp_shade(uint8_t c, uint8_t d, uint8_t s) {
    unsigned v = ((unsigned)c * d + 127u) / 255u + s;
    return (uint8_t)(v > 255u ? 255u : v);
}

/* Směs dvou barev na hraně materiálů: (a*w + b*(255-w) + 127)/255. Pro w=255 vrací a. */
static inline uint8_t rp_mix(uint8_t a, uint8_t b, uint8_t w) {
    return (uint8_t)(((unsigned)a * w + (unsigned)b * (255u - w) + 127u) / 255u);
}

/* ---------- Varianta A: indexované PNG + mtRL ---------- */

rp_status_t rp_mtrl_parse(const uint8_t *data, size_t len, rp_mtrl_t *out);

/*
 * Spočítá novou paletu. `plte` je obsah chunku PLTE (n_entries × RGB), přepisuje
 * se na místě. Položky s m1 == RP_MAT_NONE nebo s materiálem >= n_pal se nemění
 * (zůstane původní barva).
 * Tohle je jediná funkce, kterou potřebuje firmware s vlastním PNG dekodérem:
 * zavolá ji v okamžiku, kdy dekodér načte PLTE.
 */
void rp_plte_recolor(const rp_mtrl_t *m, const rp_rgb_t *pal, int n_pal, uint8_t *plte);

/*
 * Pohodlná varianta pro celý PNG soubor v RAM: najde mtRL a PLTE, přepíše PLTE
 * a přepočítá jeho CRC. IDAT se nedotkne.
 */
rp_status_t rp_png_recolor_inplace(uint8_t *png, size_t len, const rp_rgb_t *pal, int n_pal);

/*
 * ---------- Varianta B: G-buffer ----------
 *   R = D, G = S, B = m1 << 4 | m2 (0xFF = pozadí)
 *   A = pokrytí (m1 == m2) nebo váha m1 ve směsi (m1 != m2, pixel plně pokrytý)
 */

/* Přebarví `n` pixelů RGBA na místě (lze volat po řádcích z dekodéru). */
void rp_gbuffer_recolor(uint8_t *rgba, size_t n, const rp_rgb_t *pal, int n_pal);

/*
 * Streamovací QOI dekodér s přebarvením: dekóduje G-buffer QOI a rovnou
 * zapisuje výsledné RGBA do `out` (w*h*4 bajtů). Vrací RP_OK nebo RP_ERR_TRUNCATED.
 */
rp_status_t rp_qoi_gbuffer_decode(const uint8_t *qoi, size_t len, const rp_rgb_t *pal, int n_pal,
                                  uint8_t *out, size_t out_len, uint32_t *w, uint32_t *h);

uint32_t rp_crc32(const uint8_t *data, size_t len);

#endif
