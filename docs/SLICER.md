# Implementace ve sliceru

Jak vyrobit přebarvitelný náhled podle [SPEC.md](SPEC.md). Referenční implementace
je v [poc/repaint.py](../poc/repaint.py). Názvy funkcí níže odkazují tam.

## Přehled

```
render G-bufferu   →   downsample na výsledné   →   kódování
(supersampling)        rozlišení (m1, m2, w,         A: kvantizace do palety + mtRL
                       D, S, alpha)                  B: zabalení do RGBA → QOI
```

## 1. Render G-bufferu

Místo barevného obrázku se renderují **vlastnosti povrchu** pro každý vzorek:

| hodnota | jak ji získat |
|---|---|
| `hit` | zda paprsek / fragment zasáhl model |
| `mat` | index extruderu trojúhelníku. U MMU-malovaných ploch je to extruder namalovaného facetu, takže vychází přímo z dat, ze kterých slicer počítá barvy v náhledu. |
| `D` | osvětlení **bez albeda**: `ambient + Σ kd·max(n·L, 0)`, oříznuto na 0..1 |
| `S` | odlesk (Phong / Blinn), 0..1 |

Shader, který dnes kreslí náhled jako `albedo × D + S`, stačí upravit tak, aby místo
toho zapisoval `vec4(D, S, mat / 255.0, 1.0)` do RGBA8 framebufferu.

**Důležité:** G-buffer se NESMÍ renderovat s MSAA. Hardwarové „resolve“ by
zprůměrovalo indexy materiálů do nesmyslů. Vyhlazení se dělá ručně: render ve
vyšším rozlišení (POC: 4×4 vzorky na pixel) a vlastní downsample (krok 2).

POC renderuje SDF scénu raymarchingem (`render_gbuffer`). Ve sliceru to bude běžný
offscreen OpenGL render, jen s jiným shaderem.

## 2. Downsample na výsledné rozlišení

`downsample_gbuffer(gb, mix=True)`. Pro každý pixel ze `ss × ss` vzorků:

1. `alpha` = podíl vzorků, které zasáhly model.
2. Spočítat vzorky podle materiálu. `m1` = nejčastější, `m2` = druhý nejčastější
   (pokud žádný druhý není, `m2 = m1`).
3. `w = n(m1) / (n(m1) + n(m2))`. Případný třetí materiál se zanedbá.
4. `D`, `S` = průměr přes všechny zasažené vzorky. Na hraně materiálů je to stejný
   povrch, takže stín je na obou stranách prakticky stejný.

Pixel pozadí: `m1 = m2 = 255`.

## 3a. Kódování varianty A (indexované PNG)

`encode_variant_a(g)`:

1. **Skupiny.** Pixely se rozdělí podle `(min(m1,m2), max(m1,m2))`: čisté materiály
   `(m, m)` a dvojice na hranách `(a, b)`. Dvojice s méně než 12 pixely (rohy, kde se
   potkají tři materiály) se přiřadí k převažujícímu materiálu. Nestojí za vlastní barvy.
2. **Rozpočet.** 255 položek palety (položka 0 = průhledné pozadí) se rozdělí
   úměrně `sqrt(počet pixelů)`. Dvojice mají váhu 2×, protože chyba na hraně je vidět
   víc. Minimum je 6 položek pro materiál a 3 pro dvojici.
3. **Kvantizace.** V každé skupině proběhne k-means v prostoru
   `(w·D, (1−w)·D, S, 0.6·alpha)`. Barva je v těchto souřadnicích lineární
   (`out = pal[a]·x1 + pal[b]·x2 + S`), takže euklidovská vzdálenost odpovídá chybě
   barvy pro *libovolnou* budoucí paletu. Alpha má nižší váhu, protože chyba
   průhlednosti na tmavém pozadí je méně vidět.
4. **Položky.** Každé centroidě odpovídá položka `(a, b, w, D, S)` a hodnota `tRNS`.
5. **PLTE** se spočítá ze stejného celočíselného vzorce s původními barvami. Tím je
   zaručena identita (§3.1 specifikace).
6. **Zápis PNG** v pořadí `IHDR, mtRL, PLTE, tRNS, IDAT, IEND`. `IDAT` bez filtrů
   (u indexovaných obrázků je filtr 0 obvykle nejlepší), zlib úroveň 9.

Naměřené rozdělení pro testovací scénu (5 materiálů, 440×240, ~25 tisíc pixelů modelu):
181 položek pro čisté materiály a 70 pro hrany (celkem 252 včetně pozadí).

### Proč ne dithering

Dithering by zmenšil pruhování (banding) v plynulých přechodech stínu, ale rozbije
dlouhé úseky stejných indexů, na kterých stojí komprese PNG (v POC neměřeno). Na
malém displeji tiskárny pruhování s ~30–60 úrovněmi na materiál vidět není, takže
dithering v POC není. Pokud by se ukázalo potřeba, stačí ordered dithering na `D`
před přiřazením k centroidám.

## 3b. Kódování varianty B (G-buffer)

`encode_variant_b(g)`:

```
mix = m1 != m2  a  (1 − w) > (1 − alpha)       # co je na pixelu důležitější
R = round(D × 255)
G = round(S × 255)
B = m1 << 4 | (m2 if mix else m1)              # pozadí: 0xFF
A = round(w × 255) if mix else round(alpha × 255)
```

Pak se obrázek zakóduje jako QOI (nebo PNG). Max. 15 materiálů.

## 4. Kontrola kvality

Doporučený test v CI sliceru je stejný jako v POC (`make_poc.py`): vyrenderovat
„ground truth“ s několika náhodnými paletami a porovnat s přebarvením. Orientační meze
z POC (ΔE76, složeno na tmavé pozadí):

| | průměr | 99. percentil | pixely s ΔE > 5 |
|---|---|---|---|
| varianta A | < 0,7 | < 7 | < 1,5 % |
| varianta B | < 0,3 | < 5,5 | < 1,1 % |

## 5. Výkon

POC v Pythonu/numpy (M-series Mac): render 1760×960 vzorků 2,2 s, downsample 0,02 s,
kvantizace a zápis varianty A 0,55 s. Ve sliceru běží render na GPU a k-means nad
~25 tisíci pixely v C++ by měl být řádově rychlejší než v Pythonu (neměřeno).
Náhled se generuje jednou při exportu g-code, takže na výkonu nezáleží.
