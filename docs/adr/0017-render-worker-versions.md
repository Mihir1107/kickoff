# ADR 0017: Render worker versions over time (old renderer/Unicode/tzdata triples)

Status: **Accepted** (2026-10-05), with the review answers below folded in. Not implemented yet beyond
what ADR 0015 §15 and §16 built (version-keyed queues, the safety-net version check, unroutable
episodes); the rest is planned work (see "Implementation" at the end).

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

## Decision

### 1. One immutable render worker image per triple
- Every release that changes the triple builds a render worker image. The build computes the triple
  from the image itself (`python -m edisc_worker.versions`, a small command to add) and tags the image
  `edisc-render:r<renderer>-u<unicode>-tz<tzdata>`. The digest, git commit and build date go in a
  committed registry file, `deploy/render-images.json` (triple -> digest, commit, built_at, status).
- An image is admitted to the registry only if, inside that image, the **oracle corpus passes**
  (`tests/integration/corpus`: every case checked against the oracle computed from the dataset, the
  structural EML checks and `edisc-verify`) AND the golden generation of its key matches byte for
  byte. The oracle is the correctness proof; the goldens show the bytes are the ones that triple
  promised.
- **The image digest is recorded in the render's custody record:** the worker reports its image
  digest (`EDISC_WORKER_IMAGE_DIGEST`, set at build) and `render_started` carries it next to the
  triple, so a production names the exact image that made it.
- Image tags are immutable in the container registry (no overwrite, no lifecycle deletion).
- **Security rebuilds:** an old image may be rebuilt on a patched base, but only if the rebuilt image
  reports the same triple and passes the same goldens. It gets a new digest; the registry keeps both,
  and the newer one is used.

### 2. How long images are kept
- An image is kept while ANY production rendered with its triple is retained, plus one year: until
  the latest `retain_until` of those productions + 1 year. The retention extension job (ADR 0002)
  computes this and records it per triple; the registry entry shows `retain_until`.
- An image is **never deleted while a matter under legal hold has productions from it**, whatever
  the dates say.
- Removal is a recorded event: `audit.render_image_retired` (triple, digest, the last production's
  retention), only after the date above. A retired triple becomes `unavailable` in the registry (§4).
- Cost is small (an image per triple, typically a few per year); retention errs on keeping.

### 3. Which workers run
- **Current triple:** an always-on worker pool, as today.
- **Older admitted triples:** no standing workers. In v1 they are started by hand from a **runbook**
  (`docs/runbooks/render-old-triple.md`, to write with the implementation), triggered by the
  `render_unroutable` alert: look up the triple in `render-images.json`, start that image's workers on
  the triple's queue, watch the episode close (`picked_up`), stop the workers when the queue is idle.
  Automating this (a Job per triple, scale to zero) is in `docs/BACKLOG.md`.

### 4. Re-renders of an old render
- **Reproduce (new endpoint, to build):** `POST /v1/renders/{id}/reproductions` (`export.create`).
  A reproduction:
  - runs on the queue of the ORIGINAL render's triple, with its options;
  - renders in memory and **stores no output**: only the per-file hashes and sizes it computed, the
    result and its custody stream; it compares each file's SHA-256 and size with the original render's
    recorded files;
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
- **Admitted, but no worker running:** the API still accepts the render (or reproduction); it waits
  `requested` with an open `unroutable` episode (state `unroutable`, one alert per episode) until
  workers of that triple are started. It never times out into another triple. This includes the
  CURRENT triple: the API does not refuse a render because no current worker polls; the episode flags
  it.
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

## Review answers (2026-10-05)
1. Retention: while any production made with the image is retained, plus one year; never deleted while
   a matter under legal hold has productions from it.
2. Image digest recorded in the render's custody record (`render_started`); admission requires the
   oracle corpus to pass, not only the goldens.
3. Reproductions store no output: only hashes, the result and their custody stream.
4. Old workers: a manual runbook in v1, triggered by the unroutable alert; automation later (backlog).
5. The API accepts renders even when no current worker polls (unroutable episodes flag it); unknown or
   retired triples get 409 `renderer_unavailable`.

## Implementation (planned, not started)
- `edisc_worker.versions` command (prints the triple), the image build that tags by triple and runs the
  oracle corpus and goldens inside the image, `deploy/render-images.json`.
- `EDISC_WORKER_IMAGE_DIGEST` and its field in `render_started` (a custody payload addition).
- The registry check in the API (409 `renderer_unavailable`) and in the routing check (fail renders of
  retired triples).
- Image retention computed by the retention extension job; `audit.render_image_retired`.
- Reproductions (endpoint, workflow, custody stream).
- The runbook `docs/runbooks/render-old-triple.md`.
