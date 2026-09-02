# Mutation testing for the VICE backend

Used to find tests that cannot fail. A test that survives every mutation
of the code it covers is decorative: it passes whether or not the code
under test did anything.

```bash
python3 scripts/mutation/generate.py /tmp/mutations.json
python3 scripts/mutation/run.py /tmp/mutations.json /tmp/results.txt
```

Nothing is edited in place. Each mutation is applied to a fresh copy of
`src` and `tests` in a temporary directory. The working tree here is
often shared between agents, and a mutation left behind would be worse
than no measurement at all.

`run.py` first runs the **unmutated** copy once and refuses to continue
unless it is green (`check_baseline`). A suite with one pre-existing red
test kills every mutant for a reason that has nothing to do with the
mutant, and the kill rate reads as near-perfect while measuring nothing.

## The population

**This is the definition any reported count refers to.** A count without
it is not reproducible, and the numbers below are meaningless if the
population is not stated alongside them.

Source: the four VICE backend modules — `vice_lifecycle.py`,
`vice_binary.py`, `vice_elevation.py`, `vice_manager.py`. Every mutation
is derived mechanically by AST walk, never hand-picked: a hand-picked
list finds the weaknesses its author already suspected.

| kind | rule |
|---|---|
| `flagname` | string literal matching `[-+][a-z0-9][a-z0-9-]{2,}` → same with `X` appended |
| `flagpolarity` | same literals → leading `-`/`+` flipped |
| `resname` | string literal matching `[A-Z][A-Za-z0-9]{4,}` → same with `X` appended |
| `int` | integer literal 0–8 on a line containing `=` or `,` → value + 1 |
| `cmp` | single-operator comparison → `<=`→`<`, `>=`→`>`, `==`→`!=`, `is not None`→`is None` |

Four exclusions, all applied at generation time rather than tolerated in
the output:

* **f-string fragments.** `f"-drive{unit}type"` contributes the stem
  `-drive`, which nothing emits.
* **Sibling-tool argv.** `-axo` belongs to `ps`, not to VICE. Mutating it
  produces a permanent survivor, since the argv contract checks emitted
  flags against `x64sc -help` and `ps` argv is not in it.
* **Patterns absent from the source.** The `int` operator would emit
  `= 5` for `timeout=5,` (no space) and for `header[7]` (not an
  assignment at all).
* **Patterns whose first occurrence is prose.** `run.py` replaces the
  *first* textual occurrence. When that is a docstring or a comment the
  code is unchanged, the suite passes, and the row scores as a survivor.
  `generate.py` computes the docstring and comment spans and refuses
  these, printing each one it refused. At `bbb0261` that is exactly
  three, all in `vice_elevation.py`: the `"-features"` name and polarity
  mutants (the docstring at :155 precedes the code at :213) and an `int`
  mutant whose `= 0` first matched the module docstring's
  `geteuid() == 0` at :6.

### A no-op result is a defect to remove, not a row to skip

This is the rule those exclusions exist to serve, and it was learned the
expensive way.

The generator was reporting `NOTAPPLIED -drive` on **every sweep**. That
line means "this tool did nothing", and it was skipped over every time.
Tolerating it is *how* the internal-hyphen bug survived: the one output
that would have prompted a look at the extraction was the one being
routinely ignored. `-autostart-warp` went unmutated for a week behind a
row nobody read.

A tool that reports its own inaction is telling you something. If a row
means the instrument did not measure, fix the instrument.

### The count, and the revision it belongs to

The count depends on the source revision, so any figure must name one.

**At `bbb0261` (this branch, `fix/vice-backend-audit`), `generate.py`
yields exactly 168 mutations**: cmp=71, flagname=38, flagpolarity=38,
int=16, resname=5. By file: `vice_lifecycle.py` 98, `vice_binary.py` 41,
`vice_manager.py` 17, `vice_elevation.py` 12. That is what the tool
prints; if it prints something else, the source has changed and this
paragraph is stale.

The default test population is the six non-live VICE modules listed in
`run.py:DEFAULT_MODULES`; pass `--modules` to measure against a different
set, which is how "survives the non-live suite" and "survives the live
suite too" were separated.

## Measured results

**Read the provenance before quoting these.** The results below were
measured at commit `6868168`, which is on the local branch
`fix/vice-phantom-flags-and-snapshot-64k` and is **not an ancestor of this
branch**. At that revision the generator (before the hyphen fix and the
exclusions above) yielded 167 mutations (cmp=66, flagname=38,
flagpolarity=38, int=20, resname=5). Reproduce it with
`git archive 6868168 src tests` into a scratch tree, the generator *as it
stood there*, and `run.py --repo <that tree>`. The sweep has **not** been
re-run against the current population; the numbers are kept because the
corrections they record are still the point.

**Whole population, baseline suite (src *and* tests at `6868168`), against
the six non-live modules:**

| verdict | count |
|---|---|
| population | 167 |
| not applied (pattern absent from source) | 5 |
| not viable (broke collection) | 1 |
| **viable** | **161** |
| survived | 85 |
| killed | 76 |

Three of the 85 "survivors" were the docstring first-occurrence no-ops
now excluded at generation (see the fourth exclusion), so 82 is the honest
survivor count for that run.

**The flag funnel.** Population is the `flagname` and `flagpolarity`
mutations in `vice_lifecycle.py`. At `bbb0261` that is **74 mutants over
37 literals** — the *right* 37 (see correction 3 below). The staged
figures were measured on an earlier 74 that was the *wrong* 37: it
carried `-axo` and `-drive`, which are not VICE flags, and lacked
`-autostart-warp` / `+autostart-warp`. The four mutants that differ have
been accounted for separately: the two spurious literals never applied or
were out of scope, and the four `autostart-warp` mutants (name and
polarity, both signs) were tested on their own and **all die** — the two
name corruptions at the contract, the two polarity flips at the live
readback modules (`test_autostart_warp_follows_cfg_warp` and
`test_default_neutralises_an_ambient_vicerc`). So the residual is still 0
on the corrected population, and the stages below are not re-derived.

Of the 74 measured, 2 never apply, so **72 viable**:

| stage | survivors |
|---|---|
| baseline suite at `6868168` | 48 (24 killed) |
| non-live suite of that day + `test_vice_argv_contract.py` | 9 (63 killed) |
| of those, out of scope — `-axo` is a `ps` flag, not VICE's | 2 |
| **genuine residue: polarity flips** | **7** |
| after the live resource-readback modules | **0** (7 killed) |

Both polarities of a flag are valid VICE options, so a name-based guard
passes either way; only reading the resource back distinguishes them.
The last residue was `-sound` forced on by a configured `sounddev`.

### Corrections to earlier published figures

All were found by re-running with the fixed scorer or by diffing
populations, and all go the same direction — **the argv contract's
contribution was overstated**.

1. **The baseline was taken after my own change.** An earlier write-up
   gave the funnel as 58 survivors falling to 10. That 58 was measured
   against a tree from which five argv tests had *already been deleted*.
   Ten mutations flip verdict between the two baselines, and they are
   exactly the flags those five tests covered. Removing tests and then
   crediting a new guard with killing the resulting survivors is not a
   measurement of the guard. The correct baseline is **48**.
2. **One mutant was scored as a survivor when it had broken collection**
   (`vice_binary.py:128:int`) — see the scorer note below.

   An earlier write-up also gave the population as 68 with 10 killed; it
   is 74 and 24.
3. **The population is now identical to an independent extraction.**
   After the exclusions above and the regex fix, this generator and one
   built separately by another agent produce **the same 37 literals** in
   `vice_lifecycle.py` — symmetric difference empty, not merely the same
   count.

   The number went 37 (wrong set) → 39 (hyphen fix) → **37 (right set)**.
   Landing back on the number it started at, having changed which
   literals it contains, is the whole lesson in one line: a matching
   count is not a matching population.

   (An earlier revision of this file quoted the funnel population as
   "78 (39 literals)". That counted `vice_elevation.py`'s `-default` and
   `-features` — argv for the `x64sc -features` probe, not the emulator
   launch — alongside the lifecycle flags. `-features` is now excluded as
   a prose first-hit; `-default` in `vice_elevation.py` remains in the
   whole population but is not part of the funnel.)
4. **The flag regex skipped every hyphenated flag.** It was
   `[-+][a-z0-9]{3,}`, which cannot match `-autostart-warp` — a flag this
   harness emits unconditionally (`vice_lifecycle.py:514`). Two real
   flags were therefore never mutated in any sweep.

   It was found by diffing this population against one extracted
   independently, and the way it hid is worth keeping: **both extractions
   produced exactly 37 literals**, so the totals agreed while the sets
   did not. Mine was missing the two `autostart-warp` entries and
   carrying two things that are not VICE flags at all — `-axo` (argv for
   `ps`, in the sudo-child resolver) and `-drive` (the f-string stem of
   `f"-drive{unit}type"`, which never applies). Two missing, two
   spurious, totals identical. A matching count is not a matching
   population.
5. **Three "survivors" had mutated docstrings.** They are now refused at
   generation (fourth exclusion) rather than hand-subtracted from the
   result.
6. **The runner never ran a baseline.** It does now, and aborts if the
   unmutated suite is red.

## Known limits — read before quoting a number

These are real and they bound what the results can support.

- **First-occurrence replacement, measured at `bbb0261`.** `run.py`
  replaces the first textual occurrence of `old`, not the occurrence at
  the recorded line. **31 of the 168 mutations have an `old` that appears
  more than once** — 15 `cmp`, 10 `int`, 2 `flagname`, 2 `flagpolarity`,
  2 `resname`. The worst offenders are bare integers (`= 0` at 56 sites
  in `vice_binary.py` alone, `= 1` at 23) and comparisons
  (`lock is not None` at 7 sites in `vice_manager.py`,
  `self._text_sock is not None` at 6 in `vice_binary.py`).

  **The bias is towards survival**, which is the direction that inflates
  how much any new guard appears to add: the unmutated sites keep
  behaving correctly, so the defect is only half applied and the mutant
  is more likely to live.

  This corrects an earlier claim in this file that `cmp` was reliable
  because its text is distinctive. It is not — `cmp` is the *largest*
  affected category. Only single-site literals are safe, and the tool
  does not currently tell you which those are. (Anchoring the
  replacement to the recorded line would remove this limit entirely; it
  has not been done.)

- **Equivalent mutants.** Some survivors are unkillable rather than
  unguarded — a string used only as a type annotation, a guard already
  implied by a surrounding loop, dead code. The runner cannot tell these
  apart from coverage holes; that judgement is manual and belongs in the
  write-up.
- **Scoring is exit-code-authoritative, and must be.** An earlier version
  scored purely by scanning for `FAILED`/`ERROR` lines. A mutation that
  breaks the parse produces neither: pytest reports
  `ImportError while loading conftest ...` and exits **4**, so the mutant
  scored as a *survivor*. Measured — a seeded syntax error and a seeded
  bad import both scored SURVIVED under the old scorer. Text-parsing
  scorers are exposed to this; exit-code scorers are safe by
  construction. (Exit codes vary by failure mode: a broken conftest gives
  4 here, while a collection error inside a test module gives 1. Both are
  non-zero, which is why keying off the code rather than the text is what
  matters.)

- **No in-place restore guard is needed, by construction.** Each mutation
  is applied to a fresh `copytree` of `src` and `tests` in a temp dir;
  the real working tree is never written to, so there is no restore step
  that a signal could interrupt and leave a mutant behind. Do not "fix"
  this by adding one — sandboxing is the stronger property. (Another
  harness on this project did lose a `finally` to SIGTERM and left a
  truncated flag in its tree.)

- **`PYTHONPATH` provenance.** The runner sets `PYTHONPATH` to the box's
  `src`, but an installed copy of the package can still win and make a
  mutation look ineffective (or a clean run look broken). If a result
  surprises you, assert it first:

  ```python
  import c64_test_harness.backends.vice_binary as m
  assert "/box/" in m.__file__, m.__file__
  ```

- **Mutation score is not the whole question.** An argv test that asserts
  a flag name *is* killed by mutating that name, and was still wrong for
  months, because its expected value was the string its author typed. Ask
  where a test's oracle comes from as well as whether it can fail. See
  `tests/test_vice_argv_contract.py`.

The generator and runner have unit tests of their own:
`tests/test_mutation_generate.py` (the prose exclusion, against fixtures
and against the real targets) and `tests/test_mutation_run.py` (the
baseline gate, with a stub `pytest`).
