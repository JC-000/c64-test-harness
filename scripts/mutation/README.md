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

## Measured results

Reproduce the baseline with `git archive 6868168 src tests` into a scratch
tree and `generate.py --repo <that tree>`.

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

**The flag funnel.** Population is exactly the `flagname` and
`flagpolarity` mutations in `vice_lifecycle.py` — 74 of them, 2 of which
never apply, so **72 viable**:

| stage | survivors |
|---|---|
| baseline suite at `6868168` | 48 (24 killed) |
| today's non-live suite + `test_vice_argv_contract.py` | 9 (63 killed) |
| of those, out of scope — `-axo` is a `ps` flag, not VICE's | 2 |
| **genuine residue: polarity flips** | **7** |
| after the live resource-readback modules | **0** (7 killed) |

Both polarities of a flag are valid VICE options, so a name-based guard
passes either way; only reading the resource back distinguishes them.
The last residue was `-sound` forced on by a configured `sounddev`.

### Two corrections to earlier published figures

Both were found by re-running with the fixed scorer, and both go the same
direction — **the argv contract's contribution was overstated**.

1. **The baseline was taken after my own change.** An earlier write-up
   gave the funnel as 58 survivors falling to 10. That 58 was measured
   against a tree from which five argv tests had *already been deleted*.
   Ten mutations flip verdict between the two baselines, and they are
   exactly the flags those five tests covered. Removing tests and then
   crediting a new guard with killing the resulting survivors is not a
   measurement of the guard. The correct baseline is **48**.
2. **One mutant was scored as a survivor when it had broken collection**
   (`vice_binary.py:128:int`) — see the scorer note below.

An earlier write-up also gave the population as 68 with 10 killed; it is
74 and 24.

## Known limits — read before quoting a number

These are real and they bound what the results can support.

- **First-occurrence replacement, measured.** `run.py` replaces the first
  textual occurrence of `old`, not the occurrence at the recorded line.
  **32 of the 167 mutations have an `old` that appears more than once** —
  13 `cmp`, 11 `int`, 3 `flagname`, 3 `flagpolarity`, 2 `resname`. The
  worst offenders are comparisons (`self._sock is not None` at 5 sites,
  `port_lock is not None` and `off >= len(data)` at 6) and bare integers
  (`= 0` at 53 sites).

  **The bias is towards survival**, which is the direction that inflates
  how much any new guard appears to add: the unmutated sites keep
  behaving correctly, so the defect is only half applied and the mutant
  is more likely to live.

  This corrects an earlier claim in this file that `cmp` was reliable
  because its text is distinctive. It is not — `cmp` is the *largest*
  affected category. Only single-site literals are safe, and the tool
  does not currently tell you which those are.

- **Docstring hits.** A literal whose first occurrence is inside a
  docstring produces a no-op mutation that reports as a survivor. Three
  of 96 survivors were this; all three were in `vice_elevation.py`. Check
  a surprising survivor before believing it.
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
