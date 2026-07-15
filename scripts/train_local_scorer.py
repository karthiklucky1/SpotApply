"""Fine-tune the distilled local scorer on exported LLM-score triples.

Designed to run on a free Colab GPU (or any machine with a GPU; CPU works but
slowly). It does NOT import the app — copy this file + the exported JSONL to
Colab and run:

    !pip install -q sentence-transformers
    !python train_local_scorer.py --data scoring_distill.jsonl --out hirepath-scorer

Then zip the output dir and place it on the server at data/models/hirepath-scorer
(LOCAL_SCORER_PATH). The app's shadow mode picks it up automatically on the
next scoring cycle. Full runbook: docs/DISTILLATION.md.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

# Keep this builder byte-identical to app/matching/local_scorer.py::build_pair —
# duplicated (not imported) so this file is standalone on Colab.
_RESUME_SLICE = 2000
_DESC_SLICE = 2500


def build_pair(row: dict) -> tuple[str, str]:
    job_text = (f"{row['title']} at {row['company']} | {row['location']} | "
                f"remote={bool(row['remote'])}\n{(row.get('description') or '')[:_DESC_SLICE]}")
    return (row.get("resume") or "")[:_RESUME_SLICE], job_text


def load_rows(path: str) -> list[dict]:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    random.Random(42).shuffle(rows)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, help="JSONL from scripts/export_training_data.py")
    ap.add_argument("--out", default="hirepath-scorer")
    ap.add_argument("--base", default="cross-encoder/ms-marco-MiniLM-L-6-v2")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--val-frac", type=float, default=0.1)
    ap.add_argument("--eval-only", action="store_true",
                    help="skip training; evaluate the model already saved at --out")
    args = ap.parse_args()

    rows = load_rows(args.data)
    n_val = max(50, int(len(rows) * args.val_frac))
    val, train = rows[:n_val], rows[n_val:]
    print(f"{len(train)} train / {len(val)} val examples")

    if not args.eval_only:
        from sentence_transformers import CrossEncoder, InputExample
        from torch.utils.data import DataLoader

        train_samples = [InputExample(texts=list(build_pair(r)), label=float(r["score"]) / 100.0)
                         for r in train]
        model = CrossEncoder(args.base, num_labels=1, max_length=512)
        model.fit(
            train_dataloader=DataLoader(train_samples, shuffle=True, batch_size=args.batch),
            epochs=args.epochs,
            warmup_steps=int(0.1 * len(train_samples) / args.batch),
        )
        model.save(args.out)
        print(f"saved → {args.out}")

    # Validation: MAE + within-10 vs the LLM teacher — through the SAME
    # transformers+explicit-sigmoid path production inference uses
    # (app/matching/local_scorer.py). The CrossEncoder wrapper's predict()
    # activation varies across sentence-transformers versions (raw logits vs
    # sigmoid), which once produced an impossible MAE=117 — never eval through it.
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(args.out)
    eval_model = AutoModelForSequenceClassification.from_pretrained(args.out).eval().to(device)
    preds: list[float] = []
    with torch.no_grad():
        for i in range(0, len(val), 64):
            batch = [build_pair(r) for r in val[i:i + 64]]
            enc = tok([a for a, _ in batch], [b for _, b in batch], truncation=True,
                      max_length=512, padding=True, return_tensors="pt").to(device)
            logits = eval_model(**enc).logits.squeeze(-1)
            preds.extend((torch.sigmoid(logits) * 100.0).tolist())

    errs = [abs(float(p) - float(r["score"])) for p, r in zip(preds, val)]
    mae = sum(errs) / len(errs)
    within10 = 100.0 * sum(1 for e in errs if e <= 10) / len(errs)
    print(f"validation vs LLM teacher: MAE={mae:.1f} pts, within-10pts={within10:.0f}%")
    print("Rule of thumb: MAE <= 8 and within-10 >= 75% is strong enough for shadow "
          "deployment; judge the final flip on live shadow numbers, not this split.")


if __name__ == "__main__":
    main()
