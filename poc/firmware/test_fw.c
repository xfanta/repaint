/*
 * Ověří C implementaci proti testovacím vektorům z poc/make_poc.py.
 *
 *   cc -std=c99 -O2 -Wall -Wextra -o test_fw repaint_fw.c test_fw.c && ./test_fw ../../out
 */
#include "repaint_fw.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint8_t *slurp(const char *dir, const char *name, size_t *len) {
    char path[1024];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "nelze otevřít %s\n", path);
        exit(2);
    }
    fseek(f, 0, SEEK_END);
    *len = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *buf = malloc(*len);
    if (fread(buf, 1, *len, f) != *len)
        exit(2);
    fclose(f);
    return buf;
}

int main(int argc, char **argv) {
    const char *out = argc > 1 ? argv[1] : "../../out";
    const char *tests[] = {"identita", "prohozeni_slotu", "uplne_jine", "sede_odstiny"};
    int fails = 0;

    size_t a_len, b_len;
    uint8_t *png_a = slurp(out, "variant_a_indexed.png", &a_len);
    uint8_t *qoi_b = slurp(out, "variant_b_gbuffer.qoi", &b_len);

    for (size_t t = 0; t < sizeof tests / sizeof *tests; t++) {
        char name[256];
        size_t pal_len, exp_len;

        snprintf(name, sizeof name, "testvectors/%s.pal", tests[t]);
        uint8_t *pal_raw = slurp(out, name, &pal_len);
        const rp_rgb_t *pal = (const rp_rgb_t *)pal_raw;
        int n_pal = (int)(pal_len / 3);

        /* A: přepis PLTE musí dát bajt po bajtu stejný soubor jako Python */
        uint8_t *png = malloc(a_len);
        memcpy(png, png_a, a_len);
        rp_status_t st = rp_png_recolor_inplace(png, a_len, pal, n_pal);
        snprintf(name, sizeof name, "testvectors/%s_A.png", tests[t]);
        uint8_t *exp = slurp(out, name, &exp_len);
        int ok_a = st == RP_OK && exp_len == a_len && !memcmp(png, exp, a_len);
        free(exp);
        free(png);

        /* B: dekódování QOI + přebarvení == Python */
        uint32_t w, h;
        uint8_t *rgba = malloc(4u * 440 * 240);
        st = rp_qoi_gbuffer_decode(qoi_b, b_len, pal, n_pal, rgba, 4u * 440 * 240, &w, &h);
        snprintf(name, sizeof name, "testvectors/%s_B.rgba", tests[t]);
        exp = slurp(out, name, &exp_len);
        int ok_b = st == RP_OK && exp_len == 4u * w * h && !memcmp(rgba, exp, exp_len);
        free(exp);
        free(rgba);
        free(pal_raw);

        printf("%-16s A %s   B %s\n", tests[t], ok_a ? "OK" : "FAIL", ok_b ? "OK" : "FAIL");
        fails += !ok_a + !ok_b;
    }

    /* PNG bez mtRL se musí odmítnout, ne poškodit */
    size_t o_len;
    uint8_t *orig = slurp(out, "original.png", &o_len);
    rp_rgb_t dummy[1] = {{1, 2, 3}};
    rp_status_t st = rp_png_recolor_inplace(orig, o_len, dummy, 1);
    printf("%-16s %s\n", "bez mtRL", st == RP_ERR_NO_MTRL ? "OK (RP_ERR_NO_MTRL)" : "FAIL");
    fails += st != RP_ERR_NO_MTRL;

    free(orig);
    free(png_a);
    free(qoi_b);
    printf(fails ? "\n%d selhání\n" : "\nvše OK\n", fails);
    return fails != 0;
}
