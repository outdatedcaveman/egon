"""Distil a bespoke static embedding student for Egon (model2vec).

Runs a strong sentence-transformer TEACHER (bge-base by default) into a tiny
static model — a numpy lookup table — that embeds at thousands/sec on CPU with
~no RAM, which is exactly what Bruno's 8GB machine needs. The result is OURS:
re-distillable any time, and tailorable to his vault via `vocabulary` (his own
domain terms get dedicated vectors instead of being split into subwords).

  python -m lib.distill_student                # default: bge-base -> 256d student
  python -m lib.distill_student --teacher BAAI/bge-base-en-v1.5 --pca 256

Output: state/egon_student_v1 (registered in lib/reembed.MODELS as the default
embedder). Quality ~ potion-retrieval-32M but half the dimension. Bruno 2026-06-24.
"""
from __future__ import annotations

import argparse
import time

from lib import egon_paths

OUT_DIR = egon_paths.STATE_DIR / "egon_student_v1"


def distill_student(teacher: str = "BAAI/bge-base-en-v1.5", pca_dims: int = 256,
                    vocabulary: list[str] | None = None) -> dict:
    """Distil `teacher` into a static student saved to OUT_DIR. `vocabulary`
    (optional) = extra whole-word/phrase tokens from Bruno's corpus to give his
    domain terms dedicated embeddings."""
    from model2vec.distill import distill
    t0 = time.time()
    kwargs = {"model_name": teacher, "pca_dims": pca_dims}
    if vocabulary:
        kwargs["vocabulary"] = list(dict.fromkeys(v for v in vocabulary if v))
    student = distill(**kwargs)
    OUT_DIR.parent.mkdir(parents=True, exist_ok=True)
    student.save_pretrained(str(OUT_DIR))
    return {"status": "ok", "out": str(OUT_DIR), "dim": student.dim,
            "teacher": teacher, "vocab_terms": len(vocabulary or []),
            "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--teacher", default="BAAI/bge-base-en-v1.5")
    ap.add_argument("--pca", type=int, default=256)
    args = ap.parse_args()
    import json
    print(json.dumps(distill_student(args.teacher, args.pca), indent=2))
