# Title sanitiser: amendment to spec 10.1

T04 finding for the M0 gate. The sanitiser in spec 10.1 produces stems that Anki Miner misreads.
This file gives the replacement rule, the reason for each change, and every counterexample found.
Anki Miner reference: `anki_miner/utils/episode_matcher.py` at commit
`ea4a30ce2be4f57f30379ca3fe1ec438ff7fb8a5`.

## Replacement text for spec 10.1 ("Sanitiser, in order")

1. Replace `< > : " / \ | ? *` and control characters with a space.
2. Collapse every whitespace run to one space; trim both ends.
3. Replace every hyphen that has a space on both sides with `~`, so the only ` - ` in the stem is
   the one before NN.
4. Rewrite any `S<digits>` + optional separators + `E<digits>` token (either case) as
   `S<digits>~E<digits>`, because that pattern outranks ` - NN` in Anki Miner.
5. Strip trailing dots and spaces; empty becomes `Game`.
6. Steps 3 and 4 must also hold for the stem as Anki Miner reads it. Its extractor deletes
   technical tokens (`1080p`, `1280x720`, `x264`, `10bit`, `[1A2B3C4D]`, `v2`) before matching.
   Where that deletion would leave a hyphen between whitespace, the hyphen becomes `~`. Where it
   would join an `S<digits>` to an `E<digits>`, a `~` goes in directly before the `E`. Repeat until
   neither applies.

Step 6 only replaces a hyphen or inserts a `~`, so no letter or digit the user typed is lost. A
title with no hazard passes through unchanged, and the rule is idempotent. All ten rows of the
spec 10.1 table keep their stems.

Spec 18.1, contract-test paragraph: the vendored copy is `EpisodeInfo`, `_strip_technical_tokens`
and the whole `EpisodeNumberExtractor` class, in `tests/contract/anki_miner_episode_matcher.py`.
The dev script is `scripts/diff_vendored_matcher.py <anki_miner checkout>`.

## Why the spec rule fails

1. **Token deletion.** `extract_episode_info` runs `_strip_technical_tokens` on the stem first and
   matches its patterns on the result. The spec checked steps 2 and 3 on the title as typed, so a
   deleted token can bring an `S1` and an `E2` together, or put spaces on both sides of a hyphen.
   This was the known counterexample (`S1 1080p E2 - 03` read as season 1, episode 2).
2. **Order.** Spec step 2 (` - ` to ` ~ `) ran before step 4 collapsed whitespace. Tabs were safe,
   because step 1 turns control characters into spaces, but U+3000 and U+00A0 are not control
   characters. Anki Miner's `\s` matches both, so `A　-　5` gave the stem `A - 5 - 01`, read as
   episode 5.
3. **Shared spaces.** A plain `str.replace(" - ", " ~ ")` misses the second of two hyphens that share
   a space: `A - - 5` became `A ~ - 5`, read as episode 5.

## Counterexamples

Each row shows the title, the stem the spec rule built, what Anki Miner's extractor read, the
amended stem, and what the extractor reads now. Rows 1-3 are the counterexamples known before T04.
Rows 7-9 and 13 were found by the adversarial property test run against the spec rule (shrunk by
Hypothesis). `٠` is ARABIC-INDIC DIGIT ZERO: the extractor's `\d` matches any Unicode digit.

| Title | Spec-rule stem | Anki Miner read | Amended stem | Anki Miner reads |
|---|---|---|---|---|
| `S1 1080p E2` | `S1 1080p E2 - 03` | season 1, episode 2 | `S1 1080p ~E2 - 03` | 3 |
| `S1 x264 E2` | `S1 x264 E2 - 03` | season 1, episode 2 | `S1 x264 ~E2 - 03` | 3 |
| `S1 v2 E3` | `S1 v2 E3 - 04` | season 1, episode 3 | `S1 v2 ~E3 - 04` | 4 |
| `S1[0123ABCD]E2` | `S1[0123ABCD]E2 - 01` | season 1, episode 2 | `S1[0123ABCD]~E2 - 01` | 1 |
| `S1 E[0123ABCD]2` | `S1 E[0123ABCD]2 - 01` | season 1, episode 2 | `S1 ~E[0123ABCD]2 - 01` | 1 |
| `S[0123ABCD]1 v2 E2` | `S[0123ABCD]1 v2 E2 - 01` | season 1, episode 2 | `S[0123ABCD]1 v2 ~E2 - 01` | 1 |
| `S00000x000E0` | `S00000x000E0 - 01` | season 0, episode 0 | `S00000x000~E0 - 01` | 1 |
| `S0 000x000E0` | `S0 000x000E0 - 01` | season 0, episode 0 | `S0 000x000~E0 - 01` | 1 |
| `S000x0000٠000p E0` | `S000x0000٠000p E0 - 01` | season 0, episode 0 | `S000x0000٠000p ~E0 - 01` | 1 |
| `A 1080p- 5` | `A 1080p- 5 - 01` | episode 5 | `A 1080p~ 5 - 01` | 1 |
| `A -1080p 5` | `A -1080p 5 - 01` | episode 5 | `A ~1080p 5 - 01` | 1 |
| `A 1080p-x264 5` | `A 1080p-x264 5 - 01` | episode 5 | `A 1080p~x264 5 - 01` | 1 |
| `x264 000p- 0` | `x264 000p- 0 - 01` | episode 0 | `x264 000p~ 0 - 01` | 1 |
| `A` U+3000 `-` U+3000 `5` | `A - 5 - 01` | episode 5 | `A ~ 5 - 01` | 1 |
| `A` U+00A0 `-` U+00A0 `5` | `A - 5 - 01` | episode 5 | `A ~ 5 - 01` | 1 |
| `A - - 5` | `A ~ - 5 - 01` | episode 5 | `A ~ ~ 5 - 01` | 1 |
| `A -` | `A - - 01` | 1, but two ` - ` in the stem | `A ~ - 01` | 1 |

No counterexample to the amended rule has been found (see Verification).

## Design choices

- **Simulate the deletion instead of approximating it.** `session/naming.py` ports the six
  deletion patterns from `_strip_technical_tokens`, with the source file and commit in a comment.
  It deletes them from `<title> - 01` while tracking where each kept character came from, finds
  the hazard in that reading, and edits the matching character of the title. Any NN reads the same,
  because no token reaches into ` - NN` and NN is never part of a hazard.
- **Rejected: deleting the tokens from the title.** Real titles contain them. `8-Bit Armies` would
  become `Armies`.
- **Rejected: a character-class rule** (treat any character that can start or end a token as
  removable, so a hyphen is replaced when both neighbours are spaces or such characters). It needs
  a hand-derived table of token edges that goes stale without warning. It also fires on common
  visual-novel subtitles: `Tsukihime -A piece of blue glass moon-` would become
  `Tsukihime ~A piece of blue glass moon-`, because `A` can start `av1`. The simulation leaves
  that title unchanged.
- **`~` before the `E`, not the spec's rewrite.** Step 4 removes the separators between `S1` and
  `E2`. Step 6 cannot do that, because the characters between them may include a token the user
  typed. It inserts `~` before the `E` instead. Step 4 still runs first, so the table rows keep
  their spec form (`s1~e2 spaced`).
- **All whitespace becomes an ASCII space.** This includes U+3000, which is common in Japanese
  titles: `ペルソナ５　ザ・ロイヤル` becomes `ペルソナ５ ザ・ロイヤル`. This follows the spec's
  "collapse whitespace". The M0 gate can instead keep U+3000 as typed. Steps 3 and 6 already match
  any whitespace, so the contract would still hold.

## Verification

- `tests/contract/test_naming_contract.py`, run by the gate:
  - Adversarial property, 2000 examples a run. Titles mix one generator per deletion in
    `_strip_technical_tokens` (ASCII and Unicode digit runs, `pPiIİı`, codecs, bit depths, CRC32
    brackets, version markers) with S/E fragments, separators (U+3000 and NBSP included), spaced
    hyphens, brackets, digits and arbitrary Unicode. NN is 1-9999. The vendored extractor must
    return exactly NN and no season.
  - An arbitrary `st.text()` property, 1000 examples.
  - The ported deletion must equal the vendored `_strip_technical_tokens`, and its index map must
    be consistent.
  - No letter or digit may be lost.
  - Every counterexample above, at NN 1, 3, 42, 999 and 9999.
- `tests/session/test_naming.py`: the spec table, one test per step, the counterexamples, and
  idempotence.
- Offline campaign (2026-09-21, not committed): 1.8 million raw random titles built from token
  fragments, plus 200,000 Hypothesis examples, all against the amended rule. No failures.
- Mutation check: with step 6 disabled, the gate's contract test fails on its explicit examples.
  Without those examples, the generated search alone found a counterexample in 16 of 20 seeded runs
  of 2000 examples.

## Maintenance

The ported patterns and the vendored copy are both pinned to `ea4a30ce`. When Anki Miner changes
`_strip_technical_tokens`, `scripts/diff_vendored_matcher.py` reports the drift. Re-vendor, then
update the port: the port-equivalence property fails until the two match, and the adversarial
property then checks the sanitiser against the new deletions.

## Related names in `session/naming.py`

- `session_stem(title, index)`: `<sanitised title> - NN`, NN from 1 to 9999, zero-padded to two
  digits. It raises `ValueError` outside that range. `title` may already be sanitised.
- `parse_index(stem)`: NN from a stem ending in ` - ` + 1-4 ASCII digits (1-9999), else `None`. It
  also accepts `- 1` and `- 007`, so NN reservation counts a file the user renamed. It inverts
  `session_stem`.
- `slugify(title)`: the profile slug. NFKC, lower case, and every run of characters other than
  letters, digits and marks becomes `-`. Any script is kept. Empty becomes `game`, and a Windows
  device name gets `-game` appended (`con-game`). The slug always passes `is_safe_slug`. The spec
  has no slug rule; `steins-gate` in spec 5 matches this one.

## Not addressed (outside spec 10.1; for the gate to rule on)

- A title that is a Windows device name (`CON`, `NUL`, `COM1`) makes an invalid folder name on
  Windows. Stems are fine, since `CON - 01` is not reserved.
- No length cap. A long CJK title can pass the 255-byte file-name limit on Linux, and long paths can
  pass `MAX_PATH` on Windows.
- A leading dot is kept, so `.hack` makes a hidden folder on Linux.
