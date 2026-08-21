# Card Layout & Metadata Rules

Reference for the card face and for the metadata the scraper stores about it.

Two things are documented here: the seven printed regions of a card, taken from the official
"Card Layout" (카드 구성) guide, and where each of them actually comes from when scraping.

> **No OCR is involved.** An earlier draft of this file assumed the printed fields would have to be
> read off the artwork. They do not: the board's AJAX detail endpoint
> (`/skin/board/card_list_new/get_info.php`) serves every one of them as text. The images are
> artwork only; `card_metadata.py` parses the fields.

## Layout regions

The numbering follows the official diagram. The last column is the field the scraper stores.

| # | Korean | English | Where it sits on the card | Stored as |
| --- | --- | --- | --- | --- |
| 1 | 코스트 | **Cost** | Top-left corner badge | `cost` |
| 2 | 파워 | **Power** | Top-right, left of the hit icon | `power` |
| 3 | 히트 | **Hit** | Top-right, right of the diamond icon | `hit` |
| 4 | 효과 | **Effect** | Lower third of the artwork | `effect`, `trigger_text`, `keywords` |
| 5 | 카드명 및 속성 | **Card Name** and **Attribute** | Coloured name bar; element icon at its right edge | `name`, `element` |
| 6 | 종류 및 소속 | **Type** and **Affiliation** | Strip under the name bar | `type`, `affiliation` |
| 7 | 식별 번호 및 레어도 | **ID Number** and **Rarity** | Bottom-left of the black footer | `number`, `set_code`, `rarity` |

The footer also carries `© SHIFT UP Corp.` and the `NIKKE` logo. Both are print furniture, constant
across a set, and are not scraped. Two fields have no printed region at all and come only from the
detail endpoint: `product_name` (제품명, the pack the card was released in) and `ip` (the franchise).

## The detail response

`POST /skin/board/card_list_new/get_info.php` with `bo_table` and `wr_id` returns a UTF-8 HTML
fragment. Abridged, with every field the scraper reads:

```html
<h2 id='subject'>타락의 유열 샬럿</h3>          <!-- name (yes, the close tag is wrong) -->
<h2 id="type">ST08-014 / 유닛 / 화염</h3>      <!-- number / type / element -->
<td class='h3'>코스트</td><td>6</td>   <td class='h3'>레어도</td><td>SBR</td>
<td class='h3'>파워</td><td>11000</td> <td class='h3'>히트 </td><td>2</td>
<td class='h3'>소속</td><td colspan='3'>이펙트 / 레기온</td>
<td class='h3'>키워드</td><td colspan='3'>패시브, 액티브</td>
<div id='content'>
  <img src="/img/ico_psv.png" alt="패시브"> 자신의 패 1장마다 파워-1000.<br>…
  <p class="triger_box"><b>트리거</b> / 이 카드를 자신의 패에 넣는다.</p>
</div>
<tr><td class='h3'>제품명</td><td colspan='3'>… 스페셜 부스터 팩 2026 SB02</td></tr>
<tr><td class='h3'>IP</td><td colspan='3'>이터널 리턴</td></tr>
```

### Parsing rules

These are the quirks that the parser exists to absorb; each one is covered by a test in
`tests/test_card_metadata.py` against a captured fixture.

- **`-` is null.** The site prints a bare hyphen where a field does not apply (파워/히트 on a skill
  card, 소속 on an item). It becomes `NULL`, never `0` and never the string `"-"`.
- **Labels are matched stripped.** The `히트` label is emitted as `"히트 "`, with a trailing space.
- **The header tags are malformed**: `<h2 …>` opened, `</h3>` closed. A lenient parser therefore
  nests `#type` *inside* `#subject`, so the name must be read from `#subject`'s own text nodes only
  — otherwise the card number ends up appended to the card name.
- **`#type` is not guaranteed to have three parts.** Index defensively; a missing part is `NULL`.
- **The card number is not unique.** The same card is reprinted at several rarities (`BT05-071`
  exists as both UR and SPR) and each printing is its own board entry. `wr_id` is the only key. This
  is also what produces the `-01`/`-02` filename suffixes in `downloads/`.
- **Effect text embeds icons as images.** `<img alt="패시브">`, `<img alt="장착 조건: 이브">` and
  even inline element icons carry meaning the sentence depends on; each becomes a `[패시브]` marker
  rather than being dropped. `<br>` becomes a newline.
- **The trigger box is separate.** `p.triger_box` is extracted into `trigger_text`, with its
  repeated `트리거 /` label stripped, so it never runs together with the effect.
- **The board is multi-IP.** A 70-card sample returned five franchises: 이터널 리턴, 에픽세븐,
  승리의 여신: 니케, 스텔라 블레이드, 브라운더스트2. Everything is scraped; `ip` is what filters.

### Value vocabularies

Enumerated from that same 70-card sample, not guessed.

| Field | Values |
| --- | --- |
| `type` | 유닛 (unit), 스킬 (skill), 아이템 (item), 리더 (leader) |
| `element` | 화염 (fire), 번개 (lightning), 폭풍 (storm), 파도 (wave), 대지 (earth) |
| `rarity` | `C`, `R`, `SR`, `UR`, `SPR`, `SBR`, `L`, `SPL`, `SBL`, `ANL`, `P` — open set, stored as text |
| `keywords` | 액티브, 엔트리, 엑시트, 패시브, 어태커, 믹스, 크레딧, 가디언, 포지션, … — open set |
| `affiliation` | 이펙트, 럭키, 테트라, 필그림, 미실리스, 엘리시온, 콜로니, … — open set |

`type` and `element` are the only two normalised to English, in `card_metadata.CARD_TYPE_EN`
and `ELEMENT_EN`. A value missing from a map logs a warning once and leaves the `_en` column `NULL`
— the Korean is always stored, so an unmapped value shows up as something to add, never as data
loss. Rarity is left as-is: the codes are already Latin and the set grows with every promo.

## Storage

The fields land in the `cards` table, keyed on `wr_id`. The column list, and who owns that table,
are in [README.md](README.md#database).

```sql
SELECT number, name, element, cost, power, hit, rarity, keywords
FROM cards WHERE ip = '승리의 여신: 니케' ORDER BY number;
```
