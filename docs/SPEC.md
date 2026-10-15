# Specifikace: přebarvitelný náhled (Repaint) v1

Stav: **návrh**. Ověřeno v POC (Python, C, JS dávají bajtově shodné výsledky),
zatím neimplementováno ve sliceru ani ve firmware.

Dokument popisuje datový formát náhledového obrázku, který tiskárna umí přebarvit
podle skutečně zavedených filamentů. Klíčová slova MUSÍ / NESMÍ / MĚL BY mají význam
jako v RFC 2119.

1. [Pojmy](#1-pojmy)
2. [Barevný model](#2-barevný-model)
3. [Varianta A: indexované PNG + chunk mtRL](#3-varianta-a-indexované-png--chunk-mtrl)
4. [Varianta B: G-buffer RGBA](#4-varianta-b-g-buffer-rgba)
5. [Vložení do g-code](#5-vložení-do-g-code-návrh)
6. [Verzování a rozšíření](#6-verzování-a-rozšíření)

## 1. Pojmy

| pojem | význam |
|---|---|
| **materiál** | index nástroje / extruderu `Tn` v g-code, číslováno od 0 (T0 = 0). Materiál je *slot*, ne barva. Dva materiály mohou mít stejnou barvu. |
| **paleta filamentů** `pal[]` | barvy (sRGB, 8 bitů na kanál), které tiskárna sestaví podle filamentu *skutečně* přiřazeného každému nástroji. `n_pal` = počet známých barev. |
| **D** | difúzní stín 0–255: kolik světla dopadá na povrch. Nezávisí na barvě materiálu. |
| **S** | odlesk (specular) 0–255: bílá složka přičtená k barvě. |
| **m1, m2, w** | pixel na hraně dvou materiálů: převažující `m1`, druhý `m2` a váha `w` (podíl `m1`, 0–255). Uvnitř plochy platí `m1 == m2`, `w = 255`. |
| **pokrytí / alpha** | jak velkou část pixelu model zakrývá (vyhlazení vůči pozadí). |
| `RP_MAT_NONE` | 255: „žádný materiál“ (pozadí, položka, kterou nepřebarvovat). |

## 2. Barevný model

Pro každý kanál `c ∈ {r, g, b}` se **celočíselně** spočítá:

```
base_c = floor((pal[m1].c × w + pal[m2].c × (255 − w) + 127) / 255)     směs na hraně
out_c  = min(255, floor((base_c × D + 127) / 255) + S)                   stín + odlesk
```

- Pro čistý materiál (`m1 == m2` nebo `w == 255`) je `base = pal[m1]` přesně.
- Počítá se přímo v sRGB hodnotách, bez linearizace. Je to záměrné: výpočet je triviální
  a slicer generuje data stejným modelem, takže výsledky sedí.
- Implementace MUSÍ dávat výsledky shodné s tímto vzorcem včetně zaokrouhlení
  (ověřitelné testovacími vektory, viz [FIRMWARE.md](FIRMWARE.md#testování)).
  Optimalizace (např. dělení 255 přes násobení a posun) jsou povolené, jen pokud dávají
  identický výsledek pro všechny vstupy.

Proč se míchá *před* stínováním: stín D je na hraně dvou materiálů téměř stejný
(je to stejný povrch), liší se jen albedo. Míchat se proto musí barvy filamentů, které
zná až tiskárna. Hotové RGB pixely se míchat nedají, a právě proto by hranu nešlo
přebarvit z dnešního RGB náhledu.

## 3. Varianta A: indexované PNG + chunk mtRL

### 3.1 Požadavky na PNG

- `IHDR`: color type **3** (indexed), bit depth **8**, bez interlace.
- `PLTE`: N položek, 1 ≤ N ≤ 256.
- `tRNS`: MŮŽE být přítomen (průhlednost hran a pozadí). Přebarvení ho nemění.
- `mtRL`: MUSÍ být přítomen **právě jednou**, **za `IHDR` a před `PLTE`**.

Obsah `PLTE` v souboru MUSÍ být výsledkem §3.3 s paletou `orig_colors` z `mtRL`.
Soubor tedy bez úprav zobrazí původní barvy a přebarvení na původní barvy je identita
(bajt po bajtu).

### 3.2 Chunk `mtRL`

Vlastnosti jména podle PNG specifikace (§5.4):

| písmeno | velikost | význam |
|---|---|---|
| `m` | malé | **ancilární**: dekodér, který chunk nezná, ho přeskočí a obrázek zobrazí normálně |
| `t` | malé | **privátní** (není registrovaný v PNG specifikaci) |
| `R` | velké | rezervovaný bit, MUSÍ být velký |
| `L` | velké | **unsafe-to-copy**: obsah závisí na `PLTE` a indexech v `IDAT`, takže editor, který obrázek změní, chunk zahodí |

Obsah (big endian, bez zarovnání):

| offset | typ | pole | popis |
|---|---|---|---|
| 0 | u8 | `version` | `1` |
| 1 | u8 | `n_materials` | počet materiálů (nástrojů), 1–254 |
| 2 | u8[3 × n_materials] | `orig_colors` | původní barvy materiálů (RGB), ze kterých je spočítané `PLTE` |
| 2 + 3·n_mat | u16 | `n_entries` | počet položek; MUSÍ se rovnat počtu položek `PLTE` |
| 4 + 3·n_mat | u8[5 × n_entries] | `entries` | pro položku palety *i*: `m1, m2, w, D, S` |

- `m1`, `m2` jsou indexy materiálu `0..n_materials-1`. Čistý materiál: `m2 = m1`, `w = 255`.
- `m1 = 255` (`RP_MAT_NONE`): položky se přebarvení nedotkne (typicky položka 0 =
  průhledné pozadí). Ostatní pole jsou pak libovolná.
- Velikost chunku je `4 + 3·n_materials + 5·n_entries`, pro 5 materiálů a 256 položek
  je to 1 299 B.

### 3.3 Přebarvení

Pro každou položku palety `i`:

```
(m1, m2, w, D, S) = entries[i]
pokud m1 == 255 nebo m1 >= n_pal nebo m2 >= n_pal:   PLTE[i] beze změny
jinak:                                              PLTE[i] = barevný_model(m1, m2, w, D, S)   (§2)
```

Pak se přepočítá CRC chunku `PLTE`, pokud se soubor upravuje na místě. `IDAT`
a `tRNS` se nemění.

### 3.4 Validace a chování při chybě

Tiskárna MUSÍ v těchto případech obrázek zobrazit **beze změny**, ne chybu:

- `mtRL` chybí: obyčejný náhled, nepřebarvitelný,
- `version != 1`: neznámá budoucí verze,
- `n_entries` neodpovídá délce `PLTE`, nebo je chunk kratší, než udávají jeho pole,
- `mtRL` je až za `PLTE` (porušené pořadí).

Položka s `m1`/`m2 ≥ n_materials` (a `m1 ≠ 255`) je chyba zapisovače. Čtenář ji MÁ
nechat beze změny.

## 4. Varianta B: G-buffer RGBA

Obrázek RGBA, 8 bitů na kanál, kódovaný jako **QOI** (doporučeno pro tiskárny Prusa)
nebo PNG (color type 6). Kanály **nenesou barvu**:

| kanál | obsah |
|---|---|
| R | `D`, difúzní stín |
| G | `S`, odlesk |
| B | `m1 << 4 \| m2`, dva 4bitové indexy materiálu; `0xFF` = pozadí |
| A | `m1 == m2`: **pokrytí** pixelu (vyhlazení vůči pozadí)<br>`m1 != m2`: **váha `w`** materiálu `m1` ve směsi, pixel je plně pokrytý |

- Varianta B podporuje **max. 15 materiálů** (0–14). Index 15 je rezervovaný,
  takže `0xFF` je jednoznačně pozadí.
- Pixel nese *buď* hranu materiál/materiál, *nebo* hranu model/pozadí. Kde se potkají
  obě (vzácné rohy), uloží zapisovač tu, jejíž zanedbání by bylo vidět víc
  (`(1 − w) > (1 − alpha)` → směs, jinak pokrytí).
- Pixely pozadí MĚLY BY mít `R = G = 0, B = 0xFF, A = 0` (lepší komprese).

Přebarvení pixelu:

```
m1 = B >> 4, m2 = B & 15
pokud B == 0xFF:                    výstup (0, 0, 0, 0)
pokud m1 >= n_pal nebo m2 >= n_pal: výstup (0, 0, 0, 0)  (nebo náhradní barva, viz níže)
pokud m1 == m2:                     výstup = (barevný_model(m1, m1, 255, R, G), A)
jinak:                              výstup = (barevný_model(m1, m2, A, R, G), 255)
```

Varianta B v sobě nenese původní barvy. Pokud tiskárna pro nějaký materiál barvu nezná,
MĚLA BY použít barvu z metadat g-code (`; extruder_colour` / `; filament_colour`).
Referenční implementace takový pixel zprůhlední.

Varianta B **není zpětně kompatibilní**: bez přebarvení vypadá jako barevný šum.
Proto MUSÍ být uložena jako *další* náhled vedle běžného (§5), nikdy místo něj.

## 5. Vložení do g-code (návrh)

Dnešní g-code nese náhledy jako base64 bloky v komentářích (`; thumbnail begin …`,
`; thumbnail_QOI begin …`). Návrh:

| varianta | blok | poznámka |
|---|---|---|
| A | stávající PNG blok | soubor je platné PNG; tiskárna nebo Connect bez podpory ukáže původní barvy |
| B | nový blok, např. `; thumbnail_QOI_GBUF begin 440x240 <len>` | starý firmware neznámý blok ignoruje |

Materiál `n` v náhledu = nástroj `Tn` v g-code. Vazbu „nástroj → fyzický slot / cívka“
řeší tiskárna při přemapování. Náhled o ní nic neví a nepotřebuje to.

## 6. Verzování a rozšíření

- Nekompatibilní změna rozložení `mtRL` = nová hodnota `version`. Čtenář neznámou verzi
  ignoruje (§3.4).
- Varianta B nemá vlastní hlavičku. Změna jejího formátu = nové jméno bloku v g-code.
- Zvažovaná rozšíření:
  - průsvitné, metalické nebo silk filamenty (jiná odrazivost) by vyžadovaly
    per-materiálový parametr odlesku,
  - více než 15 materiálů ve variantě B (např. 16bitový kanál materiálů, nebo dvě
    roviny obrázku).
