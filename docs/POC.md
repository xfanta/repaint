# POC: struktura, spuštění, měření

## Spuštění

Potřeba: Python 3.10+, `numpy`, `Pillow`. Pro test firmware C kompilátor.

```bash
python3 poc/make_poc.py
```

```bash
cd poc/firmware && cc -std=c99 -O2 -Wall -Wextra -o test_fw repaint_fw.c test_fw.c && ./test_fw ../../out
```

Demo (z kořene repa, pak otevřít http://localhost:8791/demo/):

```bash
python3 -m http.server 8791
```

## Soubory

| cesta | obsah |
|---|---|
| [poc/repaint.py](../poc/repaint.py) | knihovna: renderer, downsample, kodéry A/B, QOI, přebarvení, baseline, metriky |
| [poc/make_poc.py](../poc/make_poc.py) | vygeneruje vše do `out/` a vypíše tabulky |
| [poc/firmware/](../poc/firmware/) | referenční C implementace pro tiskárnu + test |
| [demo/index.html](../demo/index.html) | interaktivní demo, čte soubory z `out/` |
| `out/original.png`, `.qoi` | „dnešní“ náhled, RGB s původními barvami |
| `out/variant_a_indexed.png` | varianta A |
| `out/variant_b_gbuffer.png`, `.qoi` | varianta B |
| `out/comparison.png` | přehled: ground truth / baseline / A / B pro 4 palety |
| `out/edges_detail.png` | 6× zvětšený detail hran, s mícháním a bez něj |
| `out/metrics.json` | velikosti a ΔE metriky |
| `out/testvectors/` | testovací vektory pro firmware ([FIRMWARE.md](FIRMWARE.md#testování)) |

## Testovací scéna

Malý SDF raymarcher v `render_gbuffer()` hraje roli sliceru. Scéna má 5 materiálů,
tedy 5 nástrojů jako MMU3 nebo XL:

| nástroj | barva | část modelu |
|---|---|---|
| T0 | černá | podstavec |
| T1 | oranžová | věž |
| T2 | bílá | pruhy na věži (jako MMU malování: hrana materiálů na *jednom* povrchu) |
| T3 | modrá | kopule (hrana materiálů na *rozhraní dvou* těles) |
| T4 | šedá | kuličky |

Černá, šedá a bílá jsou ve scéně záměrně: leží na stejné ose barev jako stín a odlesk,
takže bez masek nejdou od sebe odlišit.

Render: 440×240 (velikost náhledu Prusa tiskáren), 4×4 vzorky na pixel, dvě
směrová světla + ambient, Phong odlesk.

## Co se porovnává

Pro každou testovací paletu (`TESTS` v `make_poc.py`):

| metoda | co dělá |
|---|---|
| **ground truth** | scéna vyrenderovaná rovnou s novými barvami (průměr ze všech vzorků) |
| **baseline** | přebarvení dnešního RGB náhledu bez masek: pro každý pixel a materiál napasuje `c ≈ barva·d + s` (`d ∈ [0,1]`, `s ≥ 0`) a vezme materiál s nejmenší chybou |
| **A / B bez míchání hran** | pixel nese jen převažující materiál (první verze POC) |
| **A / B** | finální varianty s mícháním hran |

Metrika: **ΔE76** (euklidovská vzdálenost v CIELAB) po složení na tmavé pozadí
`#1E1E20` (podobné UI tiskárny), počítaná jen přes pixely modelu. Hodnota pod ~1
je okem nerozlišitelná, nad 5 je jasně vidět.

## Výsledky

| přemapování | baseline | A bez míchání | B bez míchání | **A** | **B** |
|---|---|---|---|---|---|
| identita | 0,41 / 3,0 % | 0,68 / 2,6 % | 0,48 / 2,5 % | 0,55 / 0,9 % | 0,22 / 0,7 % |
| prohození slotů | 0,89 / 3,8 % | 0,91 / 3,1 % | 0,69 / 2,9 % | 0,61 / 1,1 % | 0,24 / 0,8 % |
| úplně jiné barvy | 6,26 / 17,4 % | 0,98 / 3,6 % | 0,77 / 3,4 % | 0,59 / 1,2 % | 0,24 / 1,0 % |
| prohození šedých | 6,04 / 18,3 % | 0,96 / 3,6 % | 0,72 / 3,4 % | 0,67 / 1,2 % | 0,28 / 1,0 % |

(průměrné ΔE / podíl pixelů s ΔE > 5)

| soubor | bajty | base64 v g-code |
|---|---|---|
| dnešní PNG | 18 508 | 24 680 |
| dnešní QOI | 26 854 | 35 808 |
| A: indexed PNG + mtRL | 7 762 | 10 352 |
| B: G-buffer PNG | 14 314 | 19 088 |
| B: G-buffer QOI | 33 829 | 45 108 |

Poznámky:
- Baseline vychází dobře, dokud se barvy jen prohazují mezi výrazně odlišnými odstíny.
  Selže, jakmile jde o černou, šedou nebo bílou.
- Zbytek chyby u A je dán limitem 256 barev (pruhování ve stínu a hrubší kvantizace
  vah na hranách). U B jsou to hlavně rohy, kde se potkají tři materiály nebo
  materiál, materiál a pozadí.
- Míchání hran zvětšilo QOI varianty B z 29 kB na 34 kB, protože váhy na hranách
  zhoršují predikci QOI. Varianta A zůstala na ~7,8 kB.

## Omezení POC

- Scéna je syntetická (SDF), ne skutečný g-code. Barevný model (Phong v sRGB) je
  jednodušší než render v PrusaSliceru, ale princip (albedo × stín + odlesk) je stejný.
- Testováno na jednom rozlišení (440×240). Pro 16×16 ikonu přebarvení smysl nemá.
- Výkon ve firmware je odhad, na HW neměřeno.
