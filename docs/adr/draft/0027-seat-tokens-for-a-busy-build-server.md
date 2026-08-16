# 0027 — Seat tokens: how a busy build server hands out turns

- Status: draft
- Date: 2026-08-16

Product-owner decision round of 2026-08-16. It adds an admission queue
to the session protocol of ADR 0019 and makes the concurrent-session cap
an operator setting. It changes no verb and no invocation ABI; the
build-container contract is untouched.

## Context

A build server admits at most a fixed number of concurrent sessions and
refuses the rest, typed, with `session.limit-exceeded`. Three things
were wrong with that.

**The cap was a constant.** `DEFAULT_MAX_OPEN_SESSIONS = 4` had no
option in front of it and applied to both backend profiles, so the
`container` profile could run four builds at once — four containers at
`--container-memory 8g` and `--build-jobs 2` each — on a machine that
cannot feed them. Nothing enforced anything below that number, because
no limit on concurrent *invocations* exists at all.

**The `subprocess` profile needed one session, not four.** That profile
is the Home Assistant case: the build environment is the host, there is
no container, and contract §1.2 names its reduced guarantees outright —
**no per-session resource limits**. Four concurrent builds there are
four builds competing for one machine's memory with nothing between
them. Directories do not collide (every session owns its tree, the SDK
is unpacked per session, and every shared layer is read-only or the
context is refused), so this was never a correctness problem. It was an
unbounded one.

**A refusal without a turn is a lottery.** Under load, who gets in is
decided by who happened to retry at the right instant. On a private
server that is merely untidy; on a public one it means a client can be
refused all afternoon while later arrivals walk past it.

## Decision

### 1. The concurrent-session cap is an operator setting

`--max-sessions` (default 4), in the same table that drives the command
line, the environment and the defaults, so the knob cannot exist in one
of the three and not the others. A `subprocess`-profile deployment sets
it to 1; that is a configuration, not a special case in the code.

Sizing it from real load — memory in use, builds in flight — is a later
version's job. A static number is what an operator can reason about
today, and a dynamic one that guessed wrong would be a build killed for
arithmetic.

### 2. A refused client is handed a seat token and a time to come back

The refusal is unchanged in shape and gains two details:

```json
{"code": "session.limit-exceeded",
 "layer": "session",
 "retryable": true,
 "message": "…",
 "details": {"seat": "seat-…", "retry_after_seconds": 120}}
```

The client waits, and presents the token in the payload of its next
`open-session`:

```json
{"protocol_version": 2, "context_format": 2, "seat": "seat-…"}
```

The server either admits the session — the seat is consumed and the
response is an ordinary `open-session` response — or refuses again with
**the same token** and a fresh `retry_after_seconds`.

This is additive and does not move `SESSION_PROTOCOL_VERSION`. A server
that does not know seats ignores the field, and a client only ever sends
a token a server gave it; an old client that ignores the detail sees
exactly the refusal it saw before, which is why the code stays
`session.limit-exceeded` rather than becoming a new one.

**Why a token and not a held connection.** A waiting client that keeps
its socket open costs a connection slot and an inflight budget for the
whole wait, and the connection cap is one of the caps this server
announces. A token costs about forty bytes and survives a client that
closes its socket, moves network, or is a script invoked once a minute
by something else.

### 3. The wait is what the client learns — never the position

`retry_after_seconds` and nothing else. The server keeps its seats in
arrival order today, but **the order is the server's business and the
protocol must not promise it**: a later version with tariff tiers admits
a paying client at position 7 before a free one at position 2, and a
protocol that had published "you are second" would have been lying the
moment that shipped. What a client can act on is when to come back, and
that is exactly what it is told.

The number is **relative seconds, not a timestamp**. Over a wait of
minutes the least reliable clock in the system is the client's, and a
relative number needs no agreement about what time it is. The server's
own bookkeeping runs on a monotonic clock, so an NTP correction cannot
reorder the queue or resurrect an expired seat.

### 4. The wait grows with the position; the head is served fastest

```
retry_after = min(seat_retry_seconds × position, seat_retry_max_seconds)
```

with `--seat-retry-seconds` (default 60) and `--seat-retry-max-seconds`
(default 900). The position is used to *compute* the number and is never
sent. An operator turns the base up on a private server, where a queue
is rare and a chatty client is pointless, and down on a public one.

The head being the fastest poller is not a courtesy. It is what makes
decision 5 affordable.

### 5. A free slot belongs to the head of the queue

When a session ends and seats are waiting, the freed slot is **held**
for the head: a walk-in — a client with no seat — is refused in that
window and gets a seat of its own, at the back. This is the guarantee
the queue exists for. Without it, "you are first" means nothing, because
the client that happens to be dialling at that microsecond wins.

**It costs idle capacity, and the cost is the head's own retry
interval.** Held at a uniform five minutes, a server with fifteen-minute
builds would stand still about 17 % of the time. At the default base of
60 seconds the head's appointment is 60 seconds, the average handover
loses 30 of them, and the cost is about **3 %** — which is what makes
the guarantee worth having rather than an argument against it.

A seat promoted toward the front keeps the appointment it was last
given, so a seat that was told 180 seconds at position 3 can become the
head with up to that much left to run. It corrects itself at that seat's
very next poll, which is re-timed at its new position, and the slot is
almost always busy again by then anyway.

### 6. A seat expires at its own appointment plus one minute

```
expires_at = issued_at + retry_after + 60 s
```

A client that misses its appointment loses its seat; the next one moves
up. The grace is one minute and is a constant, not a knob: it exists to
absorb scheduling and network jitter, and an operator who wants a longer
leash has `--seat-retry-seconds` for it. This is what keeps the queue
honest without a "give up my seat" verb — abandonment is the normal case
early in a wait, when the client is far back and the seat harms nobody,
and a client that has already waited a quarter of an hour to reach the
front is not the one that walks away.

Consequently there is **no verb to release a seat**, and `Ctrl+C` in a
client tells the server nothing. The seat is gone within its appointment
plus a minute.

### 7. Some requests get no seat at all

A refusal that hands out a seat is a promise to serve. There has to be a
way to refuse *without* making it, and the wire needs it now rather than
after the fact:

```json
{"code": "session.no-seat", "retryable": true,
 "details": {"reason": "queue-full", "retry_after_seconds": 300}}
```

Today one reason exists: `--max-seats` (default 128) is reached, which
bounds the queue's memory and is the honest answer to a server being
used as a queue rather than as a build server. The reason that will
follow it is a per-client seat quota — a free tier may hold two seats, a
paying one five — and that is a *server-side* rule about identity, which
this server cannot express while one bearer token is one principal
(ADR 0019). The branch, its code and its shape exist now so that the
later work adds a reason and not a wire format.

`session.no-seat` is retryable: a client whose seats are used up gets
one when its others resolve.

### 8. Clients wait by default, with a bound and a way out

The waiting loop lives in the workbench
(`sessionclient._wait_for_admission`, driven through
`BuildRequest.wait_for_turn` / `max_wait_seconds` / `on_wait`), so every
client of the build API inherits it and no client depends on another. A
client waits by default — a build is a long thing and somebody who
started one wants the result — up to `--max-wait` (default **6 hours**,
`0` means no bound), and `--no-wait` fails at the first refusal instead.
A bound the next sleep would cross counts as already reached: sleeping
to a moment at which the client gives up anyway spends a person's
patience on an answer already known.

**The socket does not survive the wait.** Each attempt is a whole
connection — dial, `capabilities`, `open-session` — and the connection
is closed again before the sleep. That is what the token is *for*: a
client holding its socket open would spend one of the server's
connection slots and one of its inflight budgets for the length of
somebody else's build, and both are caps that server announces. It also
makes `Ctrl+C` clean, because the only thing outstanding during a wait
is one cancellable sleep.

The default bound is not a fairness rule. It is the answer to a client
left waiting by something that will never resolve. It lives in
`buildmethods` rather than in the session client, because it is what a
caller sets on `BuildRequest` and reading it must not cost the `remote`
extra.

Waiting is **not a build step** and reaches a renderer through a seam of
its own. Nothing is being built yet and may never be; a step bar
claiming otherwise would show progress that does not exist. What the CLI
does with it is cli ADR 0004's.

## Consequences

- The `subprocess` profile serves one session at a time by
  configuration (`--max-sessions 1`), which is the ordering ADR 0026 §2
  wrote down. Giving that profile a **fixed working path**, and with it
  a compiler cache that hits, becomes possible once the sessions are
  serialized — deliberately not done here (contract §1.2 forbids fixed
  paths in that profile for the concurrency reason that has just gone
  away, so it is a contract change and its own assignment).
- **Fairness here is per request, not per user.** One bearer token is
  one principal, so nothing distinguishes two clients, and a greedy one
  can hold several seats. Per-client authentication is a separate
  assignment; decision 7 is the seam it lands on.
- Seats live in memory. A restarted server has no seats, and the clients
  holding them are re-queued as walk-ins on their next poll — the same
  answer sessions already give, for the same reason (`SessionManager` is
  in-memory on purpose).
- Waiting for a seat consumes no session lease: a seat is not a session,
  the hard TTL and the idle timeout begin at admission, and a client
  that waited five hours starts with a full lease.
