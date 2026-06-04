# Composite Identity: Anchors, Subsets, and the Hash Boundary

Status: living design note. Phases 1, 2, 4 (anchors, destination-driven put,
register-time attribution) are implemented on `cg/composite-anchors-phase1` (PR #142,
which absorbed #143); the **view-edge model** below supersedes the earlier
selector-on-link approach and is the target for the next iteration.

## Summary

`roar get hf://.../climbmix --limit 2` produces a composite whose identity is the
`sha256-tree` over exactly the two downloaded shards, tagged `subset_of climbmix`.
A later `--limit 3` get produces a *different* digest and therefore a *different,
unrelated* composite. On a 6,000-shard dataset this yields a pile of orphan
subset-composites that look like a dataset with two files in it (the `85ad83fe`
symptom), with no materialized relationship to climbmix-the-dataset or to each
other.

This is not a bug in the hashing — content-addressed identity *should* give a
different set of bytes a different key. The gap is that there is **no anchor** (the
full-dataset key) and **no materialized containment** between a subset and the
dataset it was drawn from.

This note proposes:

1. **Anchor** — every get/put of a structural HF dataset also forms the
   full-dataset composite (`sha256-tree` over the whole manifest), computed from
   the manifest alone, *no extra download*.
2. **Subset = view by default** — a partial get links its job to the anchor plus a
   replayable **selector**; it only becomes a first-class composite node when a
   subset is *reused as a unit*.
3. **One hash boundary** — the portable/published plane is single-algo in the
   dataset's **origin hash** (sha256 for HF, blake3 for locally-produced). The
   `blake3 <-> sha256` crosswalk for downloaded shards is a **local cache, never
   published** on `get`. `put` is the one place a verified crosswalk may be
   asserted, because the producer computes both hashes from the same bytes.

The headline for review: **GLaaS changes least.** Almost all of this is roar-side.
See [Change surfaces](#change-surfaces).

## Background: two identity planes

A file that comes from HF and lands locally has two content identities:

- **Origin / portable hash** — what the source and every consumer addresses it by.
  HF publishes `sha256` LFS oids for free. This is the plane that lives on GLaaS.
- **Local hash** — `blake3`, which roar computes from the bytes. Fast, and
  rename-stable, so it is what local lineage uses to recognize "the file job X read
  is the same blob as leaf Y."

The qualifying composite hash already follows the *source's natural algorithm*
(`roar/application/composite/qualifying.py`): an HF dataset keys `sha256-tree`, a
locally-produced one keys `blake3-tree`. The `-tree` construction is one
path-sensitive canonical form (`roar/application/composite/canonical.py`); only the
content hash flowing through it varies by provenance. The builder's combiner already
picks the algorithm from the leaves — `sha256` iff every leaf is sha256, else
`blake3` (`composite_builder.py:_combiner_algorithm`).

The two planes must not be conflated on GLaaS. Keeping GLaaS single-algo per
artifact is the central constraint of this design.

## Current behavior (v2 branch `cg/composite-v2-phase1`)

- `get/service.py:_form_get_composite` builds a `sha256-tree` over the data + meta
  leaves of the fetched set, excluding boilerplate. With `--limit N` it takes the
  first N identity files (sorted by path) and tags the result
  `subset_of {dataset, selector: "first:N"}` in metadata.
- The composite leaves carry `component_algorithm="sha256"` (HF LFS oid, *asserted*;
  identity-bearing non-LFS files are re-hashed to sha256 under a 64 MiB budget,
  *verified*).
- Components are capped: `_MAX_STORED_COMPONENTS = 1000` and a 90 KB payload cap,
  so climbmix-full would store ~1,000 of 6,000 components. The **bloom covers all**
  leaves; the stored 1,000 are just the enumerable slice. The bloom is already
  **origin-keyed**: `key = f"{leaf.component_algorithm}:{leaf.digest}"`
  (`composite_builder.py:_build_membership_index_base`). This is correct and needs
  no change.
- **Gap A — no anchor.** A subset get never forms the full-dataset composite, even
  though the manifest makes it free.
- **Gap B — the crosswalk hole.** When a composite forms,
  `_materialize_get_result` sets `output_artifacts = []`: the downloaded shards get
  **no local artifact row at all**. Their `blake3` only appears later, when a
  downstream `roar run` reads the parquet — and that `blake3` has nothing to match
  the composite's `sha256` leaves. Local composite-attribution is impossible
  without first producing the sha256.

## Proposed model

### Anchor

On any get/put of a *structural* dataset (`detect()` returns a known kind), also
form the full-dataset composite over the entire manifest:

- For HF, the leaf digests are the published sha256 oids — so the anchor digest is
  computable **from the manifest alone, no data download**.
- The anchor is the canonical, host-agnostic dataset key. The key a `roar put` of
  the same bytes registers equals the key a `roar get` reproduces.
- Register-once / idempotent on the anchor digest: a get computes the digest,
  checks GLaaS; **exists and complete -> link only**; otherwise register the
  composite + first-cap components + full bloom. "Complete" must mean *components
  populated*, not merely *row exists*, so a half-registered composite (interrupted
  earlier get) gets healed rather than stranded — same shape as the size-0 heal
  (glaas-api PR #53).

### Subset: view by default, node on demand

> **Refined by [The view-edge model](#the-view-edge-model-supersedes-the-selector-on-link-decision):**
> the "view" is a **bloom-carrying selection edge**, not a selector recorded on the
> link. The text below is retained for the rationale (view-not-node, reproducibility
> via the command, immutability); the *mechanism* is the view edge.

A partial get is a **view** of the anchor, not a new dataset:

- The get job links to the **anchor** and records a replayable **selector**
  (`first:N`, or the explicit path/digest set for arbitrary subsets).
- **Reproducibility** comes from the recorded command + pinned commit: `first:N` at
  a fixed commit re-derives the exact shard list; explicit-path gets *are* the list.
- An optional **per-subset bloom** (built from just the loaded leaves) accelerates
  offline "was shard S loaded?" tests. It is an accelerator, not truth: blooms test
  but don't enumerate, and have false positives.
- Promote a subset to a **first-class composite node** only when it is *reused as a
  unit* — e.g. `roar put` of a curated subset. Then it earns its own
  `sha256-tree` identity and a materialized `member_of <anchor-digest>` edge.

Do **not** mutate an existing composite to "add" later-fetched shards. Content-
addressed nodes are immutable; a consumer that recorded a dependency on the 2-shard
subset must keep resolving to the 2-shard subset.

> **Open decision — selector truth.** `first:N` is exact and replayable. Arbitrary
> (non-prefix) subsets need their leaf set stored to be precise. Recommendation:
> store the selector as truth (exact for `first:N`), materialize an explicit leaf
> list only when the selector is not deterministically replayable, keep the bloom as
> an accelerator only.

### The hash boundary (the trust call)

**On `get` (HF -> local): keep the crosswalk local, publish only sha256.**

- Store each downloaded shard as a local artifact carrying **both** `blake3`
  (computed, *verified*) and `sha256` (the oid, *asserted*). This closes Gap B and
  is what lets local register answer "did job X read composite Z, and which one?"
  for files this machine fetched — for free, no re-hash.
- Treat that crosswalk as a **cache, not a source of truth**: it is always
  rederivable by sha256-ing local bytes, so a lost/rebuilt local DB degrades to
  "re-hash on demand," never to broken lineage.
- Do **not** publish `blake3` to GLaaS. Publishing "blake3 X == sha256 Y for
  artifact A" is an **alias-hash attestation**, not an upsert: it fuses a
  *verified-local* fact to an *asserted-remote* one, it is a claim GLaaS must store
  and defend (disputes, aliasing one artifact onto another's identity), and with
  subsets it would become a *repeated* hot-path operation. Keep GLaaS single-algo.
- The bloom lives in the **origin hash** (sha256 for HF). Unifying rule: *the bloom's
  algorithm is the one in which a querier can obtain a candidate's digest without a
  download.* HF -> sha256 (grab the oid free); local-origin -> blake3 (you hashed it).
  A blake3-keyed bloom would force fetching the very shard you are trying to avoid
  touching.

**On `put` (local -> HF): the verified-binding moment.**

- The dataset is locally produced, so its native key is `blake3-tree`. But HF stores
  it under sha256 oids, so to be addressable as the same artifact a later get will
  see, the put **must produce sha256** — a sha256 pass over the outgoing bytes.
  There is no way around it short of trusting HF's post-upload oids (asserted, and a
  round-trip), which defeats verification.
- Because the producer computes **both** hashes from the **same bytes**, the
  `blake3 <-> sha256` binding here is **verified**, not asserted. `put` is therefore
  the one legitimate place to *authoritatively* attest byte-equality and to register
  the `sha256-tree` identity, so that the producer's pre-put `blake3` lineage and the
  consumers' post-get `sha256` identity converge instead of being ships in the night.

> **Decision — express the put binding as a job edge, not an artifact alias.**
> Do not add a second hash to an artifact on GLaaS, even when verified: a stored
> `blake3 == sha256` claim still makes identity "a set of hashes with trust levels"
> on every read path. Instead the **put job** carries input = local `blake3`
> composite, output = `sha256` anchor, annotated *identity-preserving,
> producer-verified-equal*. Connectivity is preserved (the producer's `blake3`
> lineage to the published `sha256` dataset is walkable), GLaaS stays single-algo,
> and the equality claim is scoped to that put job. Cost: cross-algo lookups become a
> 1-hop traversal, not O(1).
>
> The put must hash the **actual outgoing bytes once**, producing `blake3` and
> `sha256` together, so the binding is over identical bytes (verified, not a
> stale-`blake3` + fresh-`sha256` mismatch). The recomputed `blake3` doubles as an
> integrity check against the tracked `blake3`: a mismatch means the bytes drifted
> since tracking and must **stop** the put, not publish silently.

### The residual sharp edge

A GLaaS membership test needs the candidate's digest **in the composite's origin
algo**. In the common single-machine `get -> run -> register` path this is free (the
local crosswalk already holds the sha256 for everything roar fetched). It only bites
a *third party* that has a file's `blake3` but **not the bytes** and wants to test an
sha256 bloom — impossible, since sha256 cannot be derived from blake3. Rare (if you
have the blake3 you almost always had the bytes) and acceptable, but it is the price
of the boundary and is recorded here as a known limitation.

## The view-edge model (supersedes the selector-on-link decision)

A refinement validated against the climbmix DAG: replace "record a selector on the
job->anchor link" with a uniform, composable construct. It resolves the DAG issues —
a run showing 100 shard inputs *and* the composite (prune), and "which subset / a
bloom on the job" — and the `--limit` selector weakness, in one move.

### Leaves are the only nodes; selections are bloom-carrying edges

- **Leaf** — a content-addressed file blob, referenced by its *origin digest* (sha256
  for HF). Leaves are the only graph *nodes*.
- A **view** is a *selection edge*: it points at a parent (another view, an anchor, or
  the repo) and carries **only a bloom** over the leaves it selects from that parent.
  It is an edge, not a job-like vertex, and it stores **no explicit leaf list and no
  selector** — just the bloom.
- The **anchor is itself a view**: it selects the dataset's identity-bearing leaves out
  of the HF repo (dropping boilerplate). It additionally carries the dataset's
  `sha256-tree` key (its content-addressed identity), because the anchor *is* the
  reusable dataset that gets cited; subordinate views need no tree key of their own.

Views compose — an edge linking to an edge, bottoming out at the repo's enumerable
manifest:

    job(process.py) ──consumes(bloom/100)──▶ climbmix anchor ──selects(bloom/6543)──▶ repo@commit ──▶ leaves

The run has **one** input edge (the consumes view), whose bloom says which 100 of the
anchor's 6543 leaves it touched. The anchor's own selects-edge bloom covers all 6543.
No per-shard edges; no subset list anywhere.

### Why the edge carries *only* a bloom

The exact selected set is never stored on the edge, because it is both **recoverable**
and **replayable** without it:

- **Recover** (enumerate): test the *parent's* leaves against the edge's bloom — the
  parent is the candidate universe, the bloom is the filter; this bottoms out at the
  fully-enumerable repo manifest. Modulo the bloom false-positive rate.
- **Replay** (reproduce): the recorded command (`roar get … --limit N` at a pinned
  commit) re-materializes the exact set. The command, not the edge, is the replay
  source of truth.
- **Test** ("was leaf X used?"): hash X into the edge bloom — O(1), the common query.

Because the bloom is built from the **actual** materialized/consumed leaves (not a
`first:N` rule), there is no selector to drift from structural detection — this is what
dissolves the selector-weakness finding. A bloom over the same leaf set with the same
parameters is byte-identical, so identical selections dedup naturally.

### The view-edge record (schema)

One edge type, reused for both relations:

    view_edge {
      relation:  consumes | produces | subset_of   # job<-view (input), job->view (output), view->parent
      target:    <parent_digest> | <repo@commit>
      bloom:     { filter_base64, bits, hashes, version }   # over the selected leaves
      count:     { selected, parent_total }     # UI sugar (e.g. 100 / 6543) — NOT identity
    }

A view edge is **symmetric across a job's I/O**: `consumes` attaches an *input* view
(a job read a subset of a dataset), `produces` attaches an *output* view (a job wrote
a subset/dataset). The view itself is identical either way — only which side of the
job it sits on differs. A *produced* view is the same object a downstream job
*consumes* (same digest/bloom, dedup'd), so outputs feed inputs through one construct;
a produced dataset with no upstream parent simply *is* a new anchor.

The bloom is the entire selection payload; `count` is display only. An input link is
therefore no longer just `(job, artifact, path)`.

### Bloom contract (authoritative — what every reader, incl. glaas-site, must match)

    key    = f"{algorithm}:{digest}"     # e.g. "sha256:054ddb…", lowercase hex; algorithm per leaf family
    seed   = blake3( b"roar:composite-membership:v1\0" + key ).digest()
    h1, h2 = uint64_le(seed[0:8]), uint64_le(seed[8:16]);  if h2 == 0: h2 = 1
    bit_i  = (h1 + i*h2) % bits           for i in range(hashes)
    member iff every bit_i is set

The component record carries `componentAlgorithm` — use it; do not assume blake3 or omit
the prefix. (Querying the bare digest or `blake3:<sha256>` against an `sha256` bloom is
a guaranteed false negative — the cause of the "definitely not a member" DAG bug.)

### How each command emits / composes views

- **`get hf://… [--limit N]`** — registers/links the **anchor** (selects identity leaves
  from the repo; `sha256-tree` key + bloom over all leaves, free from the manifest) and
  **produces** a view (output edge) whose bloom covers the downloaded leaves,
  `subset_of` the anchor. The blake3<->sha256 crosswalk is stored **locally only**.
- **`run`** — **consumes** an input view over each dataset it reads (bloom built from
  the leaves the tracer observed, resolved to origin digests via the local crosswalk;
  the per-shard input edges prune into this one view), and **produces** an output view
  for any dataset it writes (e.g. tokenized shards) — a new anchor or a `subset_of`
  some parent.
- **`put` -> S3/GCS** — **produces** a `blake3-tree` view (destination-natural) over the
  published files; if they were got from HF, that view `subset_of` the HF anchor via
  the crosswalk (the get->put bridge).
- **`register`** — serializes the lineage's view edges; publishes leaves under their
  **origin digest only** (never the crosswalk pair — see F1).

### What this supersedes

- "Selector mirrored into a structured field on the job->anchor link" (Decisions §2)
  -> replaced by the **bloom-carrying view edge**: no stored selector; the command is
  the replay truth and the bloom answers membership.
- The Phase-4 job->anchor *attribution* edge -> becomes the `consumes` **view** edge
  (anchor target + subset bloom), with the redundant component inputs pruned.

### Related fixes surfaced alongside (separate from the model)

- **F1** — stop publishing the crosswalk `sha256`; leaves carry only their origin digest
  on the published side.
- **F2** — preserve `component_algorithm` when the lineage path rebuilds a bloom
  (`registration.py:184`), or the bloom is keyed `blake3:<sha256>` for fully-stored HF
  anchors.
- **glaas component links** — a component digest is a *leaf*, not an artifact; do not
  link it to `/artifact/<digest>` (resolves by primary hash only -> 404).

## Change surfaces

How much each surface moves. The point of the design is that **GLaaS barely
changes** — it stays single-algo and reuses existing composite/bloom/heal surfaces.

### GLaaS (api + site) — smallest change

| Concern | Change |
| --- | --- |
| Anchor composite | **None.** An anchor is just another `sha256-tree` composite through the existing `registerComposite` path. |
| Bloom | **None.** Stored opaquely (`bloom_filter_base64`); origin-keying is roar-side. |
| Component cap | **None.** Cap already enforced; climbmix lists ~1,000 of 6,000 by design. |
| Register-once / heal | **None to small.** Idempotency on composite hash already exists; "exists-but-incomplete -> heal components" reuses the size-0 heal shape (PR #53). |
| Crosswalk (`blake3`) | **None by construction** on get — never published. |
| Subset as *view* (Direction 1) | **None.** Membership = job link to anchor + selector in job metadata. |
| Subset as *node* (Direction 2) | **Small, only if adopted.** Needs a `member_of`/`subset_of` containment edge type. Deferred until subsets are reused. |
| Put verified binding | **Small, only if Open-decision (a).** A verified alias-hash edge on put — the *one* place GLaaS would learn a second algo. Deferrable. |

### `roar run` — effectively no change

Composites never form in `run` (boundary-only; the `CompositeOutputMaterializer`
was evicted). `run` keeps hashing I/O with `blake3` as today. Composite attribution
happens later, at register, via the local crosswalk — `run` itself is untouched.

### `roar get` — most of the work lands here

- Form and register the **anchor** from the manifest (free; Gap A).
- Register downloaded shards as local artifacts carrying **`blake3` + `sha256`**
  (Gap B) — replaces today's `output_artifacts = []`.
- Link the get job to the anchor + **selector**; optional per-subset bloom.
- Idempotency: anchor-digest existence check -> link-only vs. register+heal.
- Bloom origin-keying: already done (`_build_membership_index_base`).

### `roar register` (bulk push of a local DB) — moderate

- Phase 4 (link I/O): resolve a job's `blake3` inputs through the **local crosswalk**
  to `sha256` leaves, and attribute them to the anchor/composite. This is the local
  "did job X touch a composite" resolution.
- Must **not** publish `blake3`; push only origin-hash identities + composites.
- Composite register-once / heal idempotency as above.

### `roar put` — reality check + the continuity bridge

**Reality (verified in code):** `roar put` uploads only to **S3/GCS** — there is no HF
put path (destinations are `s3://`/`gs://`; storage backends are S3, GCS, noop,
memory). S3/GCS impose no content hash, so `blake3-tree` is the *correct* published
identity and the existing `composite-blake3` (`put_composites.py:239`) is **not a
bug**. The "put publishes in the destination's algorithm" rule (decision 5) resolves
to `blake3` for every put roar can do today. A `sha256-tree` put only arises if/when an
HF (or other sha256-native) put destination is added — out of scope here.

What *is* real even for S3/GCS is the **get->put continuity gap**: get-from-HF gives a
`sha256-tree` anchor, then put-to-S3 forms a separate `blake3-tree` composite for the
same bytes — two disconnected identities. The fix is **not** to compute sha256 at put
(S3 doesn't need it) but to **bridge** the two via the put job edge:

- Keep `blake3-tree` for the S3/GCS publication (`D_b3_s3`).
- If the put set was got from HF (has a `sha256-tree` anchor, found via the local
  crosswalk), record the source anchor as the put job's input and `D_b3_s3` as its
  output — an ordinary lineage edge bridging the two hash planes. No second hash on any
  artifact; neither composite is mutated.

## Decisions and open questions

Resolved:

1. **Subset is a view, not a node.** A partial get (`roar get -n 2`) links its job to
   the anchor plus a selector; no subset composite is formed. This removes the
   `85ad83fe`-style orphans entirely: *what dataset is this?* -> the anchor; *what did
   this job pull?* -> the selector + the local shard artifacts.
2. **Selector = the recorded command, mirrored into a structured field.**
   **SUPERSEDED** by [The view-edge model](#the-view-edge-model-supersedes-the-selector-on-link-decision):
   the subset lives as a **bloom on the view edge**, not a selector field on the link.
   The recorded command remains the replay source of truth; the bloom answers
   membership; no explicit selector/leaf-list is stored on the edge.
3. **"Complete" = digest + non-zero size + bloom present.** Because components are
   capped, "all components stored" is never true for large datasets, so idempotency
   must not gate on component count — the stored list is a display sample; the bloom
   answers membership for every leaf regardless. The size-0 heal already shipped
   (glaas-api PR #53) covers the meaningful incompleteness.
4. **Put hashes outgoing bytes once, producing `blake3` + `sha256` together** — so the
   cross-algo binding is over identical bytes, and the recomputed `blake3` is an
   integrity check against the tracked `blake3` (mismatch -> stop the put).

5. **Put publishes in the destination's algorithm.** The published identity uses the
   algorithm of whichever system will address it: ingress (`get`/`register`) follows
   the source (sha256 for HF); egress (`put`) follows the destination (sha256 to HF).
   Today's put hardcodes `composite-blake3` (`put_composites.py:239`), so put-to-HF
   and a later get-from-HF produce different digests for identical bytes — the named
   current-code gap to fix first. The rule is destination-aware, not "always sha256":
   a put to a blake3-native/roar-local target keeps `blake3-tree`.

6. **Express the put binding as a job edge, not an artifact alias — bridge only at the
   composite level.** Do not add a second hash to any artifact on GLaaS, even when
   verified. The put job carries input = local `blake3` composite (`D_b3`),
   output = `sha256` anchor (`D_sha`), annotated identity-preserving /
   producer-verified-equal. `D_sha` never mutates or replaces `D_b3`.

   Register-then-put needs **no component-level reconciliation** — the resolution to
   the recurring trust problem. Two levels:
   - *Composite level*: `D_b3` and `D_sha` are distinct artifacts bridged by the put
     job edge — an ordinary input/output edge, no second hash.
   - *Component level*: `D_b3` keeps its `blake3` component records; `D_sha` gets its
     own `sha256` component records; the two lists coexist, each scoped under its
     composite, **never merged**. Nothing is upserted; no artifact gains a second
     hash; the trust problem does not reappear.

   The per-file `blake3 <-> sha256` correspondence (computed in the single put pass)
   lives only in the **local crosswalk cache**, unpublished. The continuity walk a
   consumer needs (get `D_sha` -> put job -> `D_b3` -> producing run -> upstream) runs
   entirely at the composite/job level and never needs file-to-file cross-algo links.
   Cost: duplicated component *metadata* (path/digest/size records, not bytes; capped
   ~1000/side, ~90 KB) — the price of keeping GLaaS single-algo.

   The put recomputes `blake3` (per #4) and verifies `D_b3' == D_b3` before linking
   (mismatch -> stop). The same-algo case (get from HF, put back to HF) is trivially
   idempotent: same bytes + same algo -> same digest -> link-only.

## Implementation plan

Small, independently shippable phases. Phases 1-4 are roar-only and need no GLaaS
change (anchors register through the existing composite endpoint; selectors live in
job metadata; the put binding is an ordinary job edge).

- **Phase 1 — get forms the anchor (roar).** On a structural HF get, form and
  register the full-dataset `sha256-tree` from the manifest (free for LFS-only sets).
  Replace subset-composite formation with anchor + a `subset`-as-view link: the get
  job links to the anchor and records `{anchor, selector}`. Close Gap B — register
  each downloaded shard as a local artifact carrying `blake3` + `sha256` (the local
  crosswalk cache) instead of `output_artifacts = []`.
- **Phase 2 — put identity is destination-driven (roar).** The published composite
  algorithm follows the destination. roar's only put destinations today are S3/GCS,
  which impose no content hash, so `blake3-tree` is correct and stays the default (the
  existing `composite-blake3` is right for them, not a bug). Make the choice explicit
  and destination-driven so a future sha256-native destination would form `sha256-tree`
  without a re-hardcode. No behavior change for S3/GCS.
- **Phase 3 — get->put continuity bridge (roar).** When a put publishes files that were
  got from HF (and so already have a `sha256-tree` anchor), link the put's
  `blake3-tree` S3 composite to its source anchor via the put job edge (input = source
  sha256 anchor, output = published blake3 composite), found through the local
  crosswalk. Bridges the two hash planes for the same bytes without computing sha256 at
  put time or mutating either composite.
- **Phase 4 — register-time composite attribution (roar).** In the bulk register link
  phase, resolve a job's `blake3` inputs through the local crosswalk to the right
  composite. Confirm idempotency treats a composite as present iff digest + non-zero
  size + bloom are set (the size-0 heal already covers this on GLaaS).
- **Phase 5 — GLaaS (only if needed).** Surface the put job edge / verified-equal
  annotation on the site; add a `member_of` containment edge only if subsets are ever
  promoted to nodes (deferred). No api change is required for phases 1-4.

## Non-goals

1. Making GLaaS multi-algo in general, or adding alias hashes outside the single
   `put`-verified exception.
2. Mutating existing composites to absorb later-fetched shards.
3. Re-introducing composite formation into `roar run`.
4. Changing the `-tree` canonical form or the bloom encoding.
