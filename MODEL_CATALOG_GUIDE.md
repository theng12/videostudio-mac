# Video Studio Model Catalog Guide

This document is the source-of-truth workflow for adding or updating models in
Video Studio. The Models tab is family-first: a **family** is an architecture
such as LTX-Video or CogVideoX, while a **model** is a downloadable variant
inside that family.

## Taxonomy

Use these terms consistently:

- **Family**: one architecture and pipeline behavior. Family metadata lives in
  `FAMILIES` in `app/backend/catalog.py`.
- **Variant**: one Hugging Face repository users can download. Variant metadata
  lives in `CATALOG` in the same file.
- **Mode**: one of `txt2video`, `img2video`, or `video2video`.
- **Clip profile**: the default resolution, frame count, FPS, steps, and
  guidance. The Models tab derives displayed duration as `frames / fps`.
- **Download size**: approximate full snapshot size in decimal GB. Verify this
  against the Hugging Face repository rather than guessing from parameter count.
- **Memory floor**: minimum unified memory for a practical load, not the model's
  download size. Leave room for macOS, text encoders, activations, and the UI.
- **Runtime**: all current local families use PyTorch, Diffusers, and MPS. They
  are not MLX models and must not be labeled as MLX-optimized.

The Models tab automatically groups variants by `family`, computes family mode,
resolution, duration, download, and RAM summaries, and keeps every family
visible when no filters are active.

## Add A Variant To An Existing Family

1. Confirm the repository is in Diffusers format and that its pipeline class is
   compatible with the existing family dispatch in `app/backend/video.py`.
2. Add one `ModelEntry` to `CATALOG` in `app/backend/catalog.py`.
3. Keep the entry beside the other variants in its family.
4. Run the verification checklist below.

Use this shape:

```python
ModelEntry(
    repo="owner/repository-name",
    label="Full accessible model name",
    family="existing-family-id",
    variant_label="Short comparison label",
    role="Two-to-four word purpose",
    size_gb=20.0,
    gated=False,
    min_unified_memory_gb=32,
    recommended_hardware="M-series Max 32 GB+; concise practical guidance.",
    capabilities=("txt2video", "img2video"),
    best_for="One plain-language sentence explaining why someone picks it.",
    use_cases=(
        ("good", "A task this variant handles well"),
        ("weak", "A meaningful compromise"),
        ("avoid", "A situation where another variant is safer"),
    ),
    video_defaults=_vd(
        frames=81,
        fps=16,
        steps=40,
        guidance=5.0,
        width=1280,
        height=704,
        dtype="bfloat16",
    ),
),
```

Field rules:

- `repo` is the exact case-sensitive Hugging Face repository ID. It is also the
  stable download and generation identifier; changing it creates a new cache ID.
- `label` is descriptive and can include architecture/mode details.
- `variant_label` is short because it is the primary row title inside a family.
- `role` explains the tradeoff, for example `Fast drafts`, `Practical 720p`, or
  `Highest image quality`.
- `family` must match an existing `FAMILIES` key.
- `capabilities` must match actual pipeline support. Never advertise a mode only
  because the upstream base model supports it.
- `video_defaults` must satisfy the architecture's frame rule in `_FRAME_BASE`
  in `app/backend/video.py` and should match a tested upstream recommendation.
- `dtype` must be accepted by the loader in `app/backend/video.py`.
- `use_cases` accepts only `good`, `weak`, and `avoid` kinds.
- `aliases` is optional and is useful when a repository has old or alternate IDs.

Adding a variant does not require frontend edits. The family and comparison row
are generated from catalog data.

## Add A New Family

A new family changes generation behavior, so catalog work alone is not enough.

1. Add a `Family` entry to `FAMILIES` in `app/backend/catalog.py`.
2. Choose a unique lowercase `id`, a short `monogram`, and a readable hex
   `accent`. The accent is presentation metadata only.
3. Write a concise `summary` describing the architecture and a practical
   `how_to_use` note covering frame rules, defaults, and hardware expectations.
4. Add at least one `ModelEntry` whose `family` matches the new ID.
5. Add every supported `(family, mode)` pipeline class to `_PIPELINE_CLASSES` in
   `app/backend/video.py`.
6. Add the architecture's valid frame divisor to `_FRAME_BASE`.
7. Confirm the pipeline loader and generation call support the new class. Add a
   narrow family-specific branch only when Diffusers signatures require it.
8. Ensure diagnostics report the family as wired and ready when dependencies are
   installed.
9. Update the catalog table in `README.md`.
10. Run the full verification checklist.

Family example:

```python
"family-id": Family(
    id="family-id",
    label="Family Name",
    monogram="FN",
    accent="#59d6c7",
    summary="What distinguishes this architecture and who should choose it.",
    how_to_use="Frame rule, useful defaults, prompt advice, and hardware caveat.",
),
```

## Update An Existing Entry

- Treat `repo` and `family` as stable identifiers. If upstream publishes a new
  repository, add a variant instead of silently repointing an old one.
- Update `size_gb`, defaults, and hardware guidance when upstream files or
  pipeline recommendations change.
- Keep labels user-facing. Put implementation detail in comments or this guide,
  not in the primary row title.
- Do not add frontend conditionals for one model. Add structured catalog data or
  a general helper that improves every family.
- Never make a status, family, capability, or RAM filter active by default. The
  unfiltered page must expose the complete catalog.

## Verification Checklist

Run from the repository root:

```bash
python -m py_compile app/backend/catalog.py app/backend/video.py app/backend/main.py
git diff --check
```

With the service running, verify the API:

```bash
curl -s http://127.0.0.1:47872/api/catalog
curl -s http://127.0.0.1:47872/api/generate/diagnostics
```

Confirm all of the following:

- Every `ModelEntry.family` exists in `FAMILIES`.
- Repository IDs are unique.
- Every capability has a matching `_PIPELINE_CLASSES` dispatch entry.
- Frame counts satisfy the family's `_FRAME_BASE` rule (`frames = base * n + 1`).
- Width, height, FPS, dtype, download size, and memory floor are plausible.
- `/api/catalog` includes `variant_label`, `role`, `video_defaults`, cache state,
  and the expected family metadata.
- The Models tab shows the family with all variants when filters are clear.
- Search matches the model label, repository, or `best_for` text.
- Family expand/collapse, Details, Download/Cancel, and Use model still work.
- Desktop and narrow mobile layouts have no horizontal page overflow.
- A cached model still appears in Generate and its defaults populate correctly.

For a new family or pipeline class, perform one real small generation in every
advertised mode. Catalog and diagnostics success does not prove that large model
weights load or that a pipeline call signature is correct.
