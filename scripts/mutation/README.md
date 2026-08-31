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
| `flagname` | string literal matching `[-+][a-z0-9]{3,}` → same with `X` appended |
| `flagpolarity` | same literals → leading `-`/`+` flipped |
| `resname` | string literal matching `[A-Z][A-Za-z0-9]{4,}` → same with `X` appended |
| `int` | integer literal 0–8 on a line containing `=` or `,` → value + 1 |
| `cmp` | single-operator comparison → `<=`→`<`, `>=`→`>`, `==`→`!=`, `is not None`→`is None` |

The count depends on the source revision, so any figure must name one.
At `6868168` — the revision the results below were measured at — the
generator yields exactly **167 mutations** (cmp=66, flagname=38,
flagpolarity=38, int=20, resname=5). At the time of writing HEAD yields
169, because the source has changed since. The default test
population is the six non-live VICE modules listed in
`run.py:DEFAULT_MODULES`; pass `--modules` to measure against a different
set, which is how "survives the non-live suite" and "survives the live
suite too" were separated.

## Measured results (source at `6868168`)

Reproduce with `git archive 6868168 src` into a scratch tree and
`generate.py --repo <that tree>`.

**Whole population, against the six non-live modules:** 96 survivors of
167. Three were docstring no-ops (all in `vice_elevation.py`), so **93
real survivors**.

**The flag funnel**, whose population is exactly *the `flagname` and
`flagpolarity` mutations in `vice_lifecycle.py`* — 74 of them:

| stage | count |
|---|---|
| population | 74 (72 applicable; 2 patterns not found) |
| survived the non-live suite | 58 (14 killed) |
| still alive after `test_vice_argv_contract.py` | 10 (48 killed) |
| of those, out of scope | 2 (`-axo`, a `ps` flag, not VICE's) |
| genuine residue: polarity flips | 8 |
| still alive after the live readback modules | 1 (7 killed) |
| still alive after `test_a_configured_sound_device_forces_sound_on` | **0** |

The last residue was the `-sound` forced on by a configured `sounddev`:
both `-sound` and `+sound` are valid VICE options, so a name-based guard
passes either way and only a resource readback can tell them apart.

An earlier write-up of this funnel said "68 ... killed 10". Both were
wrong — the population is 74 and 14 were killed — though the 58 survivors
and everything downstream of them were right. The numbers here are the
ones the tooling reproduces.

## Known limits — read before quoting a number

These are real and they bound what the results can support.

- **First-occurrence replacement.** `run.py` replaces the first textual
  occurrence of `old`, not the occurrence at the recorded line. For
  `flagname`/`flagpolarity`/`cmp` the text is distinctive enough that
  this is almost always the intended site; for **`int` it is not**, since
  `= 4` occurs everywhere. **Treat `int`-category results as having
  unreliable line attribution.**
- **Docstring hits.** A literal whose first occurrence is inside a
  docstring produces a no-op mutation that reports as a survivor. Three
  of 96 survivors were this; all three were in `vice_elevation.py`. Check
  a surprising survivor before believing it.
- **Equivalent mutants.** Some survivors are unkillable rather than
  unguarded — a string used only as a type annotation, a guard already
  implied by a surrounding loop, dead code. The runner cannot tell these
  apart from coverage holes; that judgement is manual and belongs in the
  write-up.
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
