"""Cloud video-generation providers (fal, …).

This package turns Video Studio into a gateway: it merges the local engine's
catalog with live/curated models from cloud providers, and routes generation
jobs whose model id is provider-prefixed (e.g. ``fal:…``) to the right adapter.
See SPEC.md at the launcher root for the full design.
"""
