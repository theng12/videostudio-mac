"""
Static catalog of video-generation models supported by Video Studio (Mac).

Each entry describes a Hugging Face repo plus metadata that helps the UI:
download size, gating status, hardware floor, supported modes, and a long-form
explainer. Models with the same `family` share an explainer in the UI.

Engine: PyTorch (MPS) + 🤗 Diffusers. Unlike Image Studio (MLX/mflux), every
model here runs through a Diffusers pipeline — see `video.py` for the per-family
dispatch. Each entry carries `video_defaults` (frames/fps/steps/guidance/size +
torch dtype) so the Generate tab can prefill sensible per-model settings.

`capabilities` is drawn from:
  - "txt2video"   : generate a clip from a text prompt
  - "img2video"   : animate a still image (first-frame / image-to-video)
  - "video2video" : transform an input clip guided by a prompt
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Family:
    id: str
    label: str
    summary: str
    how_to_use: str


FAMILIES: dict[str, Family] = {
    "ltx-video": Family(
        id="ltx-video",
        label="LTX-Video",
        summary=(
            "Lightricks' LTX-Video — a DiT video model tuned for speed. The "
            "lightest, fastest option here and the best Apple Silicon starting "
            "point. Does text-to-video and image-to-video."
        ),
        how_to_use=(
            "Frame count must be 8·n+1 (e.g. 97, 121). Good defaults: ~40 steps, "
            "guidance 3.0, 24 fps, 704×480. Write long, descriptive prompts — LTX "
            "responds well to detailed motion and camera descriptions."
        ),
    ),
    "wan22": Family(
        id="wan22",
        label="Wan 2.2",
        summary=(
            "Alibaba's Wan 2.2 family. The TI2V-5B variant is a single model that "
            "does both text-to-video and image-to-video at 720p and is the "
            "practical pick; the A14B mixture-of-experts variants are higher "
            "quality but very large."
        ),
        how_to_use=(
            "Frame count must be 4·n+1 (e.g. 81). Good defaults: ~40 steps, "
            "guidance 5.0, 16 fps. TI2V-5B handles 1280×704; the A14B experts are "
            "heavy — reserve them for the big-memory machine."
        ),
    ),
    "hunyuanvideo": Family(
        id="hunyuanvideo",
        label="HunyuanVideo",
        summary=(
            "Tencent's HunyuanVideo — a ~13B DiT with excellent motion and "
            "prompt adherence. Large and slow, but among the best open "
            "text-to-video quality. Uses the diffusers-format community repo."
        ),
        how_to_use=(
            "Frame count must be 4·n+1 (e.g. 61, 129). Good defaults: ~30 steps, "
            "guidance 6.0, 15 fps, 1280×720. Heavy on memory — designed here for "
            "the 512GB target machine."
        ),
    ),
    "cogvideox": Family(
        id="cogvideox",
        label="CogVideoX",
        summary=(
            "THUDM's CogVideoX — mature diffusers support with the broadest mode "
            "coverage: text-to-video, image-to-video, AND a dedicated "
            "video-to-video pipeline for restyling an existing clip."
        ),
        how_to_use=(
            "Frame count must be 8·n+1 (e.g. 49). Good defaults: ~50 steps, "
            "guidance 6.0, 8 fps, 720×480. The 2B variant runs in float16 and is "
            "the lightest; 5B runs in bfloat16 for better quality. For "
            "video-to-video, upload a clip and describe the target style."
        ),
    ),
}


def _vd(frames: int, fps: int, steps: int, guidance: float,
        width: int, height: int, dtype: str) -> dict:
    """Build a video_defaults dict (kept terse; serialized verbatim to the UI)."""
    return {
        "frames": frames, "fps": fps, "steps": steps, "guidance": guidance,
        "width": width, "height": height, "dtype": dtype,
    }


@dataclass(frozen=True)
class ModelEntry:
    repo: str
    label: str
    family: str
    size_gb: float          # approximate full-precision download size
    gated: bool
    min_unified_memory_gb: int = 16
    recommended_hardware: str = ""
    # Generation modes this model supports (see module docstring).
    capabilities: tuple[str, ...] = ("txt2video",)
    # Plain-English use-case description shown on the model card.
    best_for: str = ""
    # Structured per-model use cases — (kind, text), kind ∈ good|weak|avoid.
    use_cases: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Per-model UI/engine defaults (frames/fps/steps/guidance/size + dtype).
    video_defaults: dict = field(default_factory=dict)
    # Kept for serialize compatibility with the shared Models tab. Video models
    # are not MLX-quantized, so these stay at their neutral defaults.
    quantization: Optional[str] = None
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_apple_optimized(self) -> bool:
        # Video models run via PyTorch/MPS, not MLX — never "apple optimized"
        # in the MLX sense the Models-tab filter means.
        return False


CATALOG: tuple[ModelEntry, ...] = (
    # ──────────── LTX-Video ────────────
    ModelEntry(
        repo="Lightricks/LTX-Video",
        label="LTX-Video 2B (t2v + i2v)",
        family="ltx-video",
        size_gb=19.0,
        gated=False,
        min_unified_memory_gb=24,
        recommended_hardware="M2 Pro / M3 24 GB+; the lightest model here, best for first runs.",
        capabilities=("txt2video", "img2video"),
        best_for="The fastest, lightest way to get a clip out of this studio. Best for iteration, motion tests, and image-to-video animation.",
        use_cases=(
            ("good",  "Quick text-to-video drafts and camera/motion exploration"),
            ("good",  "Animating a still image (image-to-video) into a short clip"),
            ("weak",  "Fine text rendering inside the frame"),
            ("avoid", "Maximum photoreal fidelity — Wan/Hunyuan win there"),
        ),
        video_defaults=_vd(frames=97, fps=24, steps=40, guidance=3.0, width=704, height=480, dtype="bfloat16"),
    ),
    ModelEntry(
        repo="Lightricks/LTX-Video-0.9.7-distilled",
        label="LTX-Video 0.9.7 distilled (fast)",
        family="ltx-video",
        size_gb=19.0,
        gated=False,
        min_unified_memory_gb=24,
        recommended_hardware="M2 Pro / M3 24 GB+; distilled for very few-step generation.",
        capabilities=("txt2video", "img2video"),
        best_for="Distilled LTX for the fastest possible drafts — fewer steps, lower guidance.",
        use_cases=(
            ("good",  "Near-real-time previews and rapid prompt iteration"),
            ("weak",  "Final-quality detail vs the non-distilled checkpoint"),
        ),
        video_defaults=_vd(frames=97, fps=24, steps=8, guidance=1.0, width=704, height=480, dtype="bfloat16"),
    ),

    # ──────────── Wan 2.2 ────────────
    ModelEntry(
        repo="Wan-AI/Wan2.2-TI2V-5B-Diffusers",
        label="Wan 2.2 TI2V-5B (t2v + i2v, 720p)",
        family="wan22",
        size_gb=20.0,
        gated=False,
        min_unified_memory_gb=32,
        recommended_hardware="M2 Max / M3 Max 32 GB+; the practical Wan pick for Apple Silicon.",
        capabilities=("txt2video", "img2video"),
        best_for="One model for both text-to-video and image-to-video at 720p. The best quality-per-GB Wan option for a Mac.",
        use_cases=(
            ("good",  "720p text-to-video with strong motion coherence"),
            ("good",  "Image-to-video from a single reference frame"),
            ("weak",  "Slower than LTX; first load is large"),
        ),
        video_defaults=_vd(frames=81, fps=16, steps=40, guidance=5.0, width=1280, height=704, dtype="bfloat16"),
    ),
    ModelEntry(
        repo="Wan-AI/Wan2.2-T2V-A14B-Diffusers",
        label="Wan 2.2 T2V-A14B (MoE, high quality)",
        family="wan22",
        size_gb=62.0,
        gated=False,
        min_unified_memory_gb=128,
        recommended_hardware="Mac Studio M-series with 128 GB+ unified memory. Large MoE.",
        capabilities=("txt2video",),
        best_for="Top-tier Wan text-to-video quality via the 14B mixture-of-experts. Reserve for the big-memory machine.",
        use_cases=(
            ("good",  "Highest-fidelity Wan text-to-video"),
            ("avoid", "Anything under ~128 GB unified memory — it will thrash"),
        ),
        video_defaults=_vd(frames=81, fps=16, steps=40, guidance=4.0, width=1280, height=720, dtype="bfloat16"),
    ),
    ModelEntry(
        repo="Wan-AI/Wan2.2-I2V-A14B-Diffusers",
        label="Wan 2.2 I2V-A14B (image-to-video, MoE)",
        family="wan22",
        size_gb=62.0,
        gated=False,
        min_unified_memory_gb=128,
        recommended_hardware="Mac Studio M-series with 128 GB+ unified memory. Large MoE.",
        capabilities=("img2video",),
        best_for="Top-tier image-to-video: animate a reference frame with the 14B MoE.",
        use_cases=(
            ("good",  "Highest-fidelity image-to-video motion from a still"),
            ("avoid", "Under ~128 GB unified memory"),
        ),
        video_defaults=_vd(frames=81, fps=16, steps=40, guidance=4.0, width=1280, height=720, dtype="bfloat16"),
    ),

    # ──────────── HunyuanVideo ────────────
    ModelEntry(
        repo="hunyuanvideo-community/HunyuanVideo",
        label="HunyuanVideo (t2v, ~13B)",
        family="hunyuanvideo",
        size_gb=40.0,
        gated=False,
        min_unified_memory_gb=64,
        recommended_hardware="64 GB+ unified memory (M-series Max/Ultra). Slow but excellent.",
        capabilities=("txt2video",),
        best_for="Among the best open text-to-video quality — strong motion and prompt adherence. Large and slow.",
        use_cases=(
            ("good",  "Hero-shot text-to-video where quality matters more than speed"),
            ("good",  "Complex motion and camera moves"),
            ("avoid", "Quick iteration — start with LTX, finish on Hunyuan"),
        ),
        video_defaults=_vd(frames=61, fps=15, steps=30, guidance=6.0, width=1280, height=720, dtype="bfloat16"),
    ),
    ModelEntry(
        repo="hunyuanvideo-community/HunyuanVideo-I2V",
        label="HunyuanVideo-I2V (image-to-video)",
        family="hunyuanvideo",
        size_gb=40.0,
        gated=False,
        min_unified_memory_gb=64,
        recommended_hardware="64 GB+ unified memory (M-series Max/Ultra).",
        capabilities=("img2video",),
        best_for="HunyuanVideo quality applied to image-to-video — animate a still with high fidelity.",
        use_cases=(
            ("good",  "High-fidelity image-to-video from a reference frame"),
            ("avoid", "Under ~64 GB unified memory"),
        ),
        video_defaults=_vd(frames=61, fps=15, steps=30, guidance=6.0, width=1280, height=720, dtype="bfloat16"),
    ),

    # ──────────── CogVideoX ────────────
    ModelEntry(
        repo="THUDM/CogVideoX-2b",
        label="CogVideoX-2B (t2v + v2v, light)",
        family="cogvideox",
        size_gb=12.0,
        gated=False,
        min_unified_memory_gb=16,
        recommended_hardware="M-series 16 GB+. The lightest CogVideoX; runs in float16.",
        capabilities=("txt2video", "video2video"),
        best_for="The lightest video-to-video option — restyle an existing clip, or generate from text, on modest memory.",
        use_cases=(
            ("good",  "Video-to-video restyling of a short input clip"),
            ("good",  "Text-to-video on lower-memory Macs"),
            ("weak",  "Fine detail vs the 5B variant"),
        ),
        video_defaults=_vd(frames=49, fps=8, steps=50, guidance=6.0, width=720, height=480, dtype="float16"),
    ),
    ModelEntry(
        repo="THUDM/CogVideoX-5b",
        label="CogVideoX-5B (t2v + v2v)",
        family="cogvideox",
        size_gb=22.0,
        gated=False,
        min_unified_memory_gb=32,
        recommended_hardware="M-series Max/Ultra 32 GB+; bfloat16. Better quality than 2B.",
        capabilities=("txt2video", "video2video"),
        best_for="The quality pick for video-to-video and text-to-video in the CogVideoX family.",
        use_cases=(
            ("good",  "Higher-quality video-to-video restyling"),
            ("good",  "Text-to-video with good prompt adherence"),
            ("weak",  "Heavier than the 2B variant"),
        ),
        video_defaults=_vd(frames=49, fps=8, steps=50, guidance=6.0, width=720, height=480, dtype="bfloat16"),
    ),
    ModelEntry(
        repo="THUDM/CogVideoX-5b-I2V",
        label="CogVideoX-5B-I2V (image-to-video)",
        family="cogvideox",
        size_gb=22.0,
        gated=False,
        min_unified_memory_gb=32,
        recommended_hardware="M-series Max/Ultra 32 GB+; bfloat16.",
        capabilities=("img2video",),
        best_for="CogVideoX 5B tuned for image-to-video — animate a still frame.",
        use_cases=(
            ("good",  "Image-to-video from a single reference image"),
            ("weak",  "No text-only generation — needs an input image"),
        ),
        video_defaults=_vd(frames=49, fps=8, steps=50, guidance=6.0, width=720, height=480, dtype="bfloat16"),
    ),
)


def get_model(repo: str) -> Optional[ModelEntry]:
    for m in CATALOG:
        if m.repo == repo:
            return m
    return None


def serialize_model(m: ModelEntry) -> dict:
    # Compute a per-model hardware-fit verdict against the running Mac's
    # detected RAM. Imported lazily to avoid a circular import at module load.
    try:
        from . import system_info
        fit = system_info.fit_for(m.min_unified_memory_gb)
    except Exception:
        fit = None
    return {
        "repo": m.repo,
        "label": m.label,
        "family": m.family,
        "family_label": FAMILIES[m.family].label,
        "size_gb": m.size_gb,
        "gated": m.gated,
        "quantization": m.quantization,
        "min_unified_memory_gb": m.min_unified_memory_gb,
        "recommended_hardware": m.recommended_hardware,
        "apple_optimized": m.is_apple_optimized,
        "aliases": list(m.aliases),
        "capabilities": list(m.capabilities),
        "best_for": m.best_for,
        "use_cases": [{"kind": k, "text": t} for k, t in m.use_cases],
        "fit": fit,   # {state, label, hint, actual_gb, required_gb} or None
        # Video-specific per-model defaults consumed by the Generate tab.
        "video_defaults": m.video_defaults,
    }


def serialize_family(f: Family) -> dict:
    return {
        "id": f.id,
        "label": f.label,
        "summary": f.summary,
        "how_to_use": f.how_to_use,
    }
