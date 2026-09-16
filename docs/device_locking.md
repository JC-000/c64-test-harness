# Device locking: sharing one Ultimate 64 between lanes

`DeviceLock` is how independent processes take turns on one physical
Ultimate 64. It is a `fcntl.flock` on a per-device file, so the kernel
releases it even if the holder crashes, and `acquire()` queues rather
than failing.

It is also **advisory**. Nothing in the kernel, the harness, or the
firmware stops a process from driving a device it has not locked. That
one property is responsible for every incident in this document, so it
is worth stating in its strongest form:

> A lane that does not take the lock is invisible to every lane that
> does. The careful lane gets no protection from it and, until issue
> #194, no way to detect it.

## The failure this produces

Two lanes, one device (issue #194). Lane A ran pytest through
`create_manager(backend="u64", lock_timeout=...)` and took the lock
correctly every time. Lane B's runner did not use this package at all,
so it took no lock, and held a program live on the C64 for ~18 minutes.

Lane A's `run_prg` is a **load-and-run**: the firmware resets the
machine, loads the PRG and starts it. It does not interleave with
anything — it *replaces the program lane B believed it was talking to*.
Neither side gets an error.

What lane B saw was its own protocol answering nonsense: `OPEN_UDP`
returning `21,UNKNOWN COMMAND` where the same opcode answered
`81,INVALID PARAMS` correctly a minute later, and three runs aborting at
*different* scenarios each time. Device degradation is the natural
reading of that, and it is wrong. The variable was lane A's timing, not
lane B's code. A controlled comparison settled it: identical code,
identical device, three failures inside lane A's window and two clean
runs immediately outside it. Lane B came close to a physical
power-cycle, which would have destroyed the evidence while appearing to
fix the problem.

The lesson is not "lane B was careless". Lane A had also made ad-hoc
unlocked `Ultimate64Client(host)` reads all evening — read-only, so not
the cause, but the same class of mistake. **Two lanes each
half-participating is how a mechanism like this quietly stops being
one.**

## The rules

1. **Hold the lock for the whole run, not per call.** The unit of
   exclusion is the program on the machine, not the HTTP request. This
   is what a script does: everything under `scripts/` that drives a
   device holds it through `scripts/_u64_host.py` `hold_device_lock(host)`.

   **One deliberate exception: pytest runs lock per test, not per run.**
   `tests/conftest.py`'s autouse `device_lock_guard` takes the lock around
   each live test and releases it between tests, and the pytest runner
   scripts (`run_all_u64_live.py`, `run_sid_u64_live.py`,
   `run_u64_parallel_locked.py`) take no lock of their own. So a pytest
   run does **not** give whole-run exclusion: a neighbouring lane can take
   the device between two tests of your run, and device state one test
   leaves can be seen by another lane's next test. The reason is `/Temp`
   hygiene (issue #324). `Ultimate64Client` drains `/Temp` in two places.
   `close()` drains a client that leaked with no lock or nesting check,
   so `close()` still drains a leaking client under any hold. A lock
   release callback drains it as well, and that is the only drain that
   catches a client the test never closes. Those callbacks fire only on
   the **outermost** release and are held weakly. An outer hold would
   therefore defer the lock-release drain on a leak-prone device (the
   C64U) to the end of the run, and a leaked client garbage-collected
   before then would never drain at all. That trades a courtesy problem
   for a hardware-safety one. If you
   need whole-run exclusion, you need a different mechanism, not a wrapper
   around `pytest.main`.
2. **`run_prg` replaces the running program.** So does `run_crt`,
   `sid_play`, `reset()`, `reboot()`, and a `writemem` over live code.
   An unlocked `run_prg` is destructive, not merely rude.
3. **Fail closed.** If you cannot import the harness, refuse to run.
   Falling back to an unlocked run is precisely the failure being
   guarded against — and it is silent.
4. **Never diagnose a shared device alone.** Before concluding
   "degradation", check whether anyone else holds the lock, and re-run
   outside their window.

## Taking the lock

Through the manager, which locks for you:

```python
from c64_test_harness import create_manager

with create_manager(backend="u64", lock_timeout=1800.0) as mgr:
    with mgr.target() as target:
        ...
```

Or directly, for a fixture or a bench tool:

```python
from c64_test_harness import DeviceLock, DeviceLockTimeout

lock = DeviceLock(host)
try:
    lock.acquire_or_raise(timeout=120.0)   # structured error, not a bare False
except DeviceLockTimeout as e:
    # e.holder_pid / e.pid_alive / e.lockfile_age_seconds /
    # e.device_reachable_rest.  Do NOT reboot the device on a timeout.
    raise
try:
    ...
finally:
    lock.release()
```

`lock_timeout` bounds the wait against **wedged or dead** holders only.
A live, progressing holder extends a waiter's deadline indefinitely, so
a neighbour running a multi-hour suite does not time you out.

## Rescuing a self-held wait from another thread

An `acquire()` on a lock **its own thread already holds** (through a
second `DeviceLock` instance, without `allow_nested=True`) cannot end its
own wait: the thread that would have to call `release()` is the one
blocked. Such a wait never extends its deadline, and since issue #273 it
is also capped.

Another thread holding a reference to the holder may release it
mid-wait, and the blocked acquire then succeeds. That rescue is supported
**within the grace only**: `_SELF_HELD_WAIT_GRACE` = 2.0 s. When the
caller's timeout is longer than that, the acquire logs a WARNING naming
the device, the requested timeout and the cap, and the timeout is capped
at 2.0 s. A helper that releases later than that is too late: `acquire()`
returns `False` and `acquire_or_raise()` raises `DeviceLockTimeout`. A
timeout shorter than the grace is served as given; the grace is a cap,
never a floor.

There is no opt-out keyword and no environment variable for this
(owner decision on #277, 2026-09-14). The measured cliff sits exactly at
the constant: a helper releasing at 1.95 s rescues, one releasing at
2.20 s does not. If what you actually have is one owner re-entering the
library while it already holds the device, pass `allow_nested=True`
instead; that joins the hold rather than waiting on it.

## The acquire budget: `U64_DEVICE_LOCK_TIMEOUT`

Where a caller does not pass a timeout, the budget comes from the
environment (issue #233):

| Call | Explicit argument | `U64_DEVICE_LOCK_TIMEOUT` unset |
|---|---|---|
| `DeviceLock.acquire()` / `acquire_or_raise()` | `timeout=` | 30 s (`DEFAULT_ACQUIRE_TIMEOUT`) |
| `create_manager()` / `UnifiedManager` | `lock_timeout=` | 60 s (`unified_manager.DEFAULT_LOCK_TIMEOUT`) |

- **An explicit argument always wins**, and when one is given the
  variable is not read at all. An explicit value is not checked against
  the rules below, with one exception: NaN raises `ValueError`, because a
  NaN deadline never expires. An explicit `inf` waits for ever, and zero or
  less makes a single attempt.
- **The variable is read at call time**, on every acquire. A long-lived
  manager sees a change.
- **Neither default moved.** A caller that set nothing gets exactly what
  it got before.
- **Malformed, zero or negative, or non-finite is fatal**:
  `DeviceLockTimeoutConfigError`, a `ValueError`, so an `except
  TimeoutError` retry arm will not swallow a typo. It is raised before the
  lock is tried, and on the manager path before the device pool probes
  any device. `U64_DEVICE_LOCK_TIMEOUT` values of `30m`, `0`, `inf` and
  `nan` are all refused, including by the live-test guard in
  `tests/conftest.py`, which reads the variable through the same resolver
  with its own 300 s default. A budget of
  zero fails every queued run at once, and an infinite one makes a wedged
  device look the same as a busy one.
- **Empty (`U64_DEVICE_LOCK_TIMEOUT=`) means unset**: the default, plus
  one WARNING saying so.

This is the harness's first environment variable that is a *budget*
rather than a gate: it changes how long a wait may last, never whether
anything runs. What it bounds is unchanged, too. A live, progressing
holder still extends the deadline indefinitely, so raising the budget
buys time against wedged or dead holders and against the handoff-chain
bound, not against one long healthy run.

## Seeing the wait: progress lines and `on_wait`

A blocked acquire reports every 30 s, whether or not its deadline is
being extended. Before #233 a wait behind a non-extending holder said
nothing until it timed out. Two forms carry the same four fields:

- **A log line** from `c64_test_harness.backends.device_lock`:
  `DeviceLock <host>: still waiting after Ns; holder pid=P, lockfile
  age=As, queue depth=D; <state>`. It is logged at INFO while the deadline
  is being extended (the existing extension WARNING already covers that
  case) and at WARNING otherwise.
- **A callback**: `acquire(..., on_wait=cb)` and `acquire_or_raise(...,
  on_wait=cb)` call `cb(elapsed, holder_pid, lockfile_age, queue_depth)`
  from the waiting thread. It is never called for an uncontended acquire.
  `queue_depth` counts the caller too. An exception the callback raises
  propagates out of `acquire`, abandons the wait and deregisters the
  waiter, so a caller can use it to cancel.

`lockfile_age` is the field that separates "healthy long run" from
"wedged". The line's `<state>` says `STALE, holder may be wedged` only
when two things hold: acquire is **not** extending on that poll, and the
age exceeds `progress_window`. That is the same threshold acquire extends
on, checked against acquire's own decision. So a progress line can never
call a holder wedged while acquire is still extending behind it. The
other states are "deadline extended", "held by this thread" (the
self-held cap above, which still logs its own WARNING),
"`progress_window=None`", "handoff chain", and "holder not progressing".

`DeviceLockTimeout` and its diagnostics are unchanged.

## Checking without adopting the package

For a runner that wants to be a good neighbour without restructuring
itself around the harness:

```python
import sys

try:
    from c64_test_harness import device_lock_holder, device_lock_path
except ImportError as exc:
    # Fail closed.  An unlocked run is worse than no run: it is a run
    # whose results you cannot trust and whose damage you will attribute
    # to the device.
    sys.exit(f"refusing to run: c64-test-harness unavailable ({exc})")

holder = device_lock_holder(HOST)
if holder is not None:
    sys.exit(
        f"device {HOST} is held by PID {holder['pid']} right now "
        f"({device_lock_path(HOST)}); not starting"
    )
```

`device_lock_holder()` costs one `open`, one non-blocking shared
`flock`, and one small read. No network traffic, no blocking, and it
cannot steal the lock from a real acquirer.

Checking is not holding, though. Between the check and your first write
somebody else can acquire. If your run matters, take the lock.

## The trap: `read_info()` is not the current holder

Both lanes in #194 independently got this wrong, so it gets its own
section.

**`release()` deliberately does not unlink the lockfile.** Deleting it
would race a process that has already opened the path and is about to
`flock` it: flocks are per-inode, re-creating the file yields a new
inode, and the two processes would then hold independent locks on the
same device. So the file is left behind on purpose.

The consequence:

> **A lockfile naming a dead PID is the normal state after any completed
> run.** It is not a stale lock, not a leak, and not a wedged holder.
> There is nothing to clean up.

(An unheld lockfile is swept by the next `acquire()` on *any* device, via
`cleanup_stale()`, which proves nobody holds it by taking the flock
first. A manual `rm` cannot prove that and must not be used.)

Two methods read that file and they answer different questions:

| | Question answered | Consults the flock? | Reports a finished run as a holder? |
|---|---|---|---|
| `DeviceLock.read_info()` | who held it **last** | no | **yes — this is the trap** |
| `device_lock_holder(host)` / `DeviceLock.foreign_holder(host)` | who holds it **now** | yes | no |

`read_info()` is the one a wrapper author reaches for first, and it
gives a confidently wrong answer — the same class of answer the lock
exists to prevent. It is for diagnostics only. Build on
`device_lock_holder()`.

`DeviceLock.held_by_this_process(host)` answers the other half ("is it
*us*?") from an in-process registry, at the cost of a dict lookup.

Note also that the lock record is live status only: it is a single
last-writer-wins slot, and unheld files are swept by unrelated acquires.
Nothing in this package can tell you which lane held a device an hour
ago.

## Releasing the lock can now make network calls

Since the `/Temp` hygiene work, releasing a `DeviceLock` is no longer
purely local. Each `Ultimate64Client` registers its device's `/Temp`
ledger (`ultimate64_temp_gc.TempLedger.drain_on_lock_release`) through
`device_lock.register_release_callback`. The release path fires it **while
the flock is still held**, so the device is still exclusively ours when
the drain runs. Nested acquires fire it only on the outermost release. The
ledger is registered once per host, not once per client, so one release
drains once however many clients the process built (issue #295). The
callback registry holds weak references, and the ledger holds its clients
weakly, so registering never keeps a client alive.

Two consequences for a lane author: a release may take FTP round trips
on a leak-prone device (it is best-effort and swallows its own errors —
it never fails the run, so **a clean release does not prove a clean
device**), and it is *why* handing the device to the next
lane clean is automatic rather than something each lane remembers to do.
The mechanism, the budget and the failure modes are in
[`docs/u64_recovery.md`](u64_recovery.md) § "Harness-side mitigation:
FTP `/Temp` GC".

## The two warnings, and why there are two

### `advisory_lock_check` — at destructive-call time

Called before every non-GET request. It warns when **this** process
does not hold the lock and **another live process does** — and raises
`DeviceLockContentionError` instead under `U64_REQUIRE_DEVICE_LOCK=1`.
Single-user flows never see it.

Its blind spot is exactly the #194 shape: a colliding lane that never
took the lock is not a "live holder", so there is nothing for this check
to see.

### The unlocked-client notice — at construction time

Constructing an `Ultimate64Client` while this process holds no lock for
that host logs one WARNING, once per process per host, naming the
lockfile. It does not require anyone else to be visible; the point is to
tell *you* that *you* are unlocked, before the collision rather than
after.

Silence it with `U64_UNLOCKED_CLIENT_WARNING=0`, or per client with
`Ultimate64Client(host, warn_unlocked=False)`. Code that must build a
client just before acquiring the lock can wrap that construction in
`suppress_unlocked_warning()` — the harness's own `_LockedU64Manager`
does, because the inner pool chooses the device (and builds the
transport) before the host to lock is known. All three suppress a
*message*, never a check.

### Why the client does not just take the lock itself

It was considered and rejected. `Ultimate64Client` has many downstream
consumers for whom construction is not the unit of exclusion, and a
constructor that acquires a cross-process lock has its own failure modes
(when is it released? what happens on a 30-minute queue inside
`__init__`? what about the read-only probe?). The lock belongs to the
run, and the run is the caller's to scope.

## Related

- `docs/u64_recovery.md` — wedge tiers and what to do instead of a
  power-cycle. Read it before concluding a shared device is broken.
- `CLAUDE.md` § "Destructive U64E endpoints and the poweroff guard".
