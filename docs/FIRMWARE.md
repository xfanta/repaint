# Implementace ve firmware tiskárny

Jak náhled podle [SPEC.md](SPEC.md) přebarvit v tiskárně. Referenční implementace
v C99 je v [poc/firmware/](../poc/firmware/): bez alokací, bez floatu, ověřená proti
testovacím vektorům.

## Tok v tiskárně

```
otevření g-code
   │
   ├─ sestavit paletu pal[Tn] podle filamentu přiřazeného každému nástroji
   │     (po přemapování / spool join; neznámý nástroj → vynechat, n_pal = počet známých)
   │
   ├─ náhled typu A (PNG s chunkem mtRL)?
   │     ano → přepsat PLTE (rp_plte_recolor / rp_png_recolor_inplace) → běžné dekódování
   │     ne  → zobrazit beze změny
   │
   └─ náhled typu B (blok thumbnail_QOI_GBUF)?
         ano → dekódovat QOI s přebarvením na výstupu (rp_qoi_gbuffer_decode)
         ne  → použít běžný náhled
```

Vždy platí: **když cokoli nesedí, zobrazí se původní obrázek beze změny.** Přebarvení
je vylepšení, kvůli kterému nesmí náhled zmizet.

Odkud vzít barvu filamentu (profil, NFC tag cívky, ruční volba uživatele), je mimo
rozsah tohoto návrhu. Formátu stačí pole `pal[Tn]`.

## Varianta A: indexované PNG

Barvy pixelů se nepočítají. Přepíše se jen paleta, max. 256 položek, a to ve chvíli,
kdy ji dekodér načte.

### Integrace do streamovacího PNG dekodéru (doporučeno)

Chunk `mtRL` je v souboru **před** `PLTE` (vynuceno specifikací), takže dekodér ho
zná dřív, než potřebuje paletu:

1. Při parsování chunků si `mtRL` uložit do bufferu (max. `4 + 3·n_mat + 5·256` B,
   pro 5 materiálů 1,3 kB).
2. Po načtení `PLTE` zavolat `rp_mtrl_parse()` + `rp_plte_recolor()` nad bufferem
   palety dekodéru.
3. CRC chunku `PLTE` ověřovat *před* přepisem (pokud ho dekodér vůbec ověřuje).

Data obrázku (`IDAT`) jdou dekodérem beze změny. Žádná práce navíc na pixel.

### Celý soubor v RAM

`rp_png_recolor_inplace(png, len, pal, n_pal)` najde `mtRL` a `PLTE`, přepíše paletu
a přepočítá CRC. Výsledek je platné PNG pro libovolný dekodér.

## Varianta B: G-buffer QOI

Přebarvení se dělá **na výstupu** QOI dekodéru, pixel po pixelu:

```c
m1 = B >> 4;  m2 = B & 15;
if (B == 0xFF)       → průhledný pixel
else if (m1 == m2)   → rgb = shade(pal[m1], D=R, S=G),               alpha = A
else                 → rgb = shade(mix(pal[m1], pal[m2], w=A), R, G), alpha = 255
```

**Pozor:** stav QOI dekodéru (předchozí pixel, tabulka 64 posledních barev) MUSÍ
zůstat v hodnotách G-bufferu. Přebarvuje se až kopie zapisovaná na výstup. Kdyby se
přebarvil pixel, ze kterého dekodér počítá další rozdíly, rozpadne se zbytek obrázku.
Tak to dělá `rp_qoi_gbuffer_decode()`.

Pokud dekodér zapisuje po řádcích, stačí na každý hotový řádek zavolat
`rp_gbuffer_recolor(row, width, pal, n_pal)`.

## API (`repaint_fw.h`)

| funkce | popis |
|---|---|
| `rp_mtrl_parse(data, len, &m)` | rozparsuje obsah chunku `mtRL`; ukazatele míří do původního bufferu |
| `rp_plte_recolor(&m, pal, n_pal, plte)` | přepočítá paletu na místě; položky s neznámým materiálem nechá |
| `rp_png_recolor_inplace(png, len, pal, n_pal)` | totéž nad celým souborem PNG včetně CRC |
| `rp_gbuffer_recolor(rgba, n, pal, n_pal)` | přebarví `n` pixelů G-bufferu na místě |
| `rp_qoi_gbuffer_decode(qoi, len, pal, n_pal, out, out_len, &w, &h)` | QOI dekodér s přebarvením na výstupu |
| `rp_shade(c, D, S)`, `rp_mix(a, b, w)` | inline barevný model (§2 specifikace) |

Návratové kódy: `RP_OK`, `RP_ERR_NO_MTRL` (obyčejné PNG), `RP_ERR_BAD_MTRL`
(neznámá verze, poškozený chunk), `RP_ERR_MISMATCH` (velikost PLTE nesedí),
`RP_ERR_NOT_PNG`, `RP_ERR_NO_PLTE`, `RP_ERR_TRUNCATED`. Při čemkoli jiném než
`RP_OK` zobrazit obrázek beze změny.

## Náklady

Velikost kódu, `arm-none-eabi-gcc 16.1 -mcpu=cortex-m4 -mthumb -Os`:

| část | funkce | `.text` |
|---|---|---|
| A, streamovací integrace | `rp_mtrl_parse` + `rp_plte_recolor` | 292 B |
| A, celý soubor | + `rp_png_recolor_inplace` + `rp_crc32` | 590 B |
| B, přebarvení | `gbuf_px` + `rp_gbuffer_recolor` | 290 B |
| B, vlastní QOI dekodér | `rp_qoi_gbuffer_decode` | 420 B (firmware QOI dekodér už má) |

RAM: žádná heap alokace. A potřebuje buffer na `mtRL` (≤ 1,3 kB pro 5 materiálů)
při streamování, nebo nic navíc při úpravě souboru na místě. B nepotřebuje nic
kromě palety `3 × n_pal` B.

CPU (odhad, neměřeno na HW):
- **A:** ≤ 256 položek × 3 kanály × (2 násobení + 2 dělení konstantou). Zanedbatelné,
  jednou na obrázek.
- **B:** na pixel 3 násobení a 3 dělení konstantou 255, na hranových pixelech (v testovací scéně 4 % pixelů modelu)
  dvojnásobek. Pro 440×240 je to ~105 tisíc pixelů. Dělení konstantou překladač nahradí
  násobením, takže jde o desítky cyklů na pixel, tedy řádově jednotky ms na MCU
  ve stovkách MHz.

## Testování

`poc/make_poc.py` generuje do `out/testvectors/` pro každou testovací paletu:

| soubor | obsah |
|---|---|
| `<jméno>.pal` | paleta, `n_pal × RGB` |
| `<jméno>_A.png` | očekávaný soubor po `rp_png_recolor_inplace` (bajt po bajtu) |
| `<jméno>_B.rgba` | očekávané RGBA po dekódování a přebarvení `variant_b_gbuffer.qoi` |

Spuštění testu referenční implementace:

```bash
cd poc/firmware && cc -std=c99 -O2 -Wall -Wextra -o test_fw repaint_fw.c test_fw.c && ./test_fw ../../out
```

Očekávaný výstup:

```
identita         A OK   B OK
prohozeni_slotu  A OK   B OK
uplne_jine       A OK   B OK
sede_odstiny     A OK   B OK
bez mtRL         OK (RP_ERR_NO_MTRL)

vše OK
```

Firmwarová implementace (např. s optimalizovaným dělením) MUSÍ projít stejnými
vektory. Shodu výsledků Python ↔ C ↔ JS (demo) jsme ověřili.
