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
   exclusion is the program on the machine, not the HTTP request.
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
