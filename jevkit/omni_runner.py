"""Run by the Jev-Omni environment's own Python, never imported by Hermes (see omni_vision.run_omni).

stdin:  {"items": [{"id", "image", "state", "question", "options", "region"?, "image_tokens"?}]}
stdout: one JSON line {"load_s", "results": [{"id", "prediction", "confidence", "ms", "peak_gb"} | {"id", "error"}]}

The model is loaded from ``<omni dir>/models/Jev-Omni-MLX-8bit`` through that directory's
``omni_mlx.py``. Only ids, classes and numbers are written out; nothing is logged to disk.
A ``region`` [x1, y1, x2, y2] is cropped first, as the vision tool does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--omni-dir", required=True)
    parser.add_argument("--model")
    args = parser.parse_args()
    request = json.loads(sys.stdin.read() or "{}")
    sys.path.insert(0, args.omni_dir)
    started = time.perf_counter()
    try:
        from omni_mlx import JevOmniMLX  # type: ignore
        from PIL import Image  # type: ignore

        model = JevOmniMLX(args.model or os.path.join(args.omni_dir, "models", "Jev-Omni-MLX-8bit"))
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"error": f"load_failed:{type(error).__name__}"}))
        return 0
    load_s = round(time.perf_counter() - started, 2)
    results = []
    for item in request.get("items") or []:
        crop = None
        try:
            image = item["image"]
            region = item.get("region")
            if isinstance(region, list) and len(region) == 4:
                with Image.open(image) as picture:
                    x1, y1, x2, y2 = (int(v) for v in region)
                    handle = tempfile.NamedTemporaryFile(prefix="jev-vision-crop-", suffix=".png", delete=False)
                    handle.close()
                    picture.convert("RGB").crop((x1, y1, x2, y2)).save(handle.name)
                    crop = image = handle.name
            answer = model.predict(item.get("state") or "", item["question"], item["options"], [image],
                                   int(item.get("image_tokens") or 280))
            results.append({"id": item.get("id"), "prediction": answer["prediction"],
                            "confidence": answer["confidence"],
                            "probabilities": answer.get("probabilities"), "ms": answer.get("ms"),
                            "peak_gb": answer.get("peak_gb")})
        except Exception as error:  # noqa: BLE001
            results.append({"id": item.get("id"), "error": type(error).__name__})
        finally:
            if crop:
                try:
                    os.unlink(crop)
                except OSError:
                    pass
    print(json.dumps({"load_s": load_s, "results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
