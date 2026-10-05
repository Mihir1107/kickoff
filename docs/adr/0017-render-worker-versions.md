# ADR 0017: Render worker versions over time (old renderer/Unicode/tzdata triples)

Status: **Draft for review** (2026-10-05). Nothing here is implemented beyond what ADR 0015 §15 and
§16 already built (version-keyed queues, the safety-net version check, unroutable episodes).

## Context
- A render's bytes are promised for its inputs and its **triple**: renderer version, Unicode version
  (fixed by the pinned Python patch release) and tzdata version (ADR 0015 §6). The triple is recorded
  on the render, in `render_started`, in every file's headers and in the golden key.
- Renders run only on the task queue of their triple, `renders.r<renderer>.u<unicode>.tz<tzdata>`
  (ADR 0015 §15). A worker serves exactly the triple of its runtime. A render whose triple no worker
  serves becomes `unroutable` after `EDISC_RENDER_UNROUTABLE_SECONDS`, with one alert per episode
  (§16).
- Releases move the triple forward: a renderer change that alters bytes (a version bump), a tzdata
  update, a Python patch upgrade. Productions made with older triples stay under retention for the
  life of their matter, potentially years.
- Someone may need an old render reproduced: a court, opposing counsel or our own audit asks whether
  the production delivered in 2026 can be regenerated, byte for byte, from the collected evidence.
  That needs a worker running the old triple.

## Decision (proposed)

### 1. One immutable render worker image per triple
- Every release that changes the triple builds a render worker image. The build computes the triple
  from the image itself (`python -m edisc_worker.versions`, a small command to add) and tags the image
  `edisc-render:r<renderer>-u<unicode>-tz<tzdata>`. The digest, git commit and build date go in a
  committed registry file, `deploy/render-images.json` (triple -> digest, commit, built_at, status).
- An image is admitted to the registry only if, inside that image, the RSMF golden tests of its
  `golden_key()` pass byte for byte. The golden generation is the proof that the image renders what
  that triple promises.
- Image tags are immutable in the container registry (no overwrite, no lifecycle deletion).
- **Security rebuilds:** an old image may be rebuilt on a patched base, but only if the rebuilt image
  reports the same triple and passes the same goldens. It gets a new digest; the registry keeps both,
  and the newer one is used.

### 2. How long images are kept
- An image is kept while ANY production rendered with its triple is retained: until the latest
  `retain_until` of those productions, plus one year. The retention extension job (ADR 0002)
  computes this and records it per triple; the registry entry shows `retain_until`.
- An image is never removed while a matter holding such a production is open, held or reopenable.
- Removal is a recorded event: `audit.render_image_retired` (triple, digest, the last production's
  retention), only after the date above. A retired triple becomes `unavailable` in the registry (§4).
- Cost is small (an image per triple, typically a few per year); retention errs on keeping.

### 3. Which workers run
- **Current triple:** an always-on worker pool, as today.
- **Older admitted triples:** no standing workers. Workers start on demand from the archived image
  (for example a Kubernetes Job or ECS task per triple). Each polls the queue of its triple and exits
  after an idle period. The trigger is the unroutable episode of §16: the routing check opens an
  episode for a render of an old triple, and an operator (later: automation) starts that triple's
  workers from the registry. The episode ends when the render is picked up.

### 4. Re-renders of an old render
- **Reproduce (new endpoint, to build):** `POST /v1/renders/{id}/reproductions` (`export.create`).
  A reproduction:
  - runs on the queue of the ORIGINAL render's triple, with its options;
  - renders in memory, stores nothing, and compares each file's SHA-256 and size with the original
    render's recorded files;
  - has its own custody stream: `reproduction_started` (references the render and its seal),
    `reproduction_completed` with `reproduced` or `differs` (per-file results), then sealed.
  - `differs` is an integrity incident with an alert.
- **A new render of an old job** is not a re-render: it takes the triple of the current API and
  workers (a new identity) and is a different production.
- A reproduction never runs on another triple. The queue keys by triple and the activities re-check
  the triple (the safety net of ADR 0015 §15), so there is no path to newer versions.

### 5. When no worker can exist
- **Triple unknown or retired** (not admitted to `render-images.json`, or marked `unavailable`):
  - the API refuses a reproduction at once: 409 `renderer_unavailable`, naming the triple. Nothing is
    created, and the refusal is audited.
  - a render already in the queue for such a triple (one created before its image was retired) is
    failed by the routing check: `render_failed`, reason `renderer_unavailable`, with an alert.
- **Admitted, but no worker running:** the render waits `requested` with an open `unroutable`
  episode (state `unroutable`, one alert per episode) until workers of that triple are started. It
  never times out into another triple.
- **Under no condition** does a render or a reproduction fall back to newer (or any other)
  versions.

### 6. Release process
- A triple change is a release step:
  1. build and admit the new image (goldens pass);
  2. deploy workers of the new triple;
  3. only then deploy the API that creates renders with that triple.
- This order avoids creating renders no worker can serve. If the order is broken anyway, the
  unroutable episodes make it visible.

## Consequences
- Plus: any production can be reproduced for as long as it is retained, on exactly the triple that made
  it, or the system says plainly that it cannot (409 or a failed render with a reason), never silently.
- Plus: the golden generation per triple is the admission test, so an image that drifted cannot serve.
- Minus: image storage and the operational step of starting old workers on demand.
- Minus: old images carry old dependencies. Security rebuilds are allowed only when they keep the
  triple and pass the goldens; if no patched rebuild can pass, the old image is run isolated
  (no network but Temporal, Postgres and the evidence bucket).

## Open questions for review
1. The retention rule: latest production retention plus one year, or a fixed minimum (for example
   seven years) on top?
2. Reproductions: store nothing (proposed), or store the reproduced files as a second production
   for comparison?
3. Starting old workers: manual runbook first (proposed), or automation in the same milestone?
4. Should the API refuse a new render when no worker of the current triple polls (rather than
   creating an unroutable render)?
