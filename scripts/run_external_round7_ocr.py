from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


TERMS = ("ສປ ຈີນ", "ສປຈີນ", "ລາວ-ຈີນ", "ຈີນ-ລາວ", "ຈີນ", "china", "chinese")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconstruct_text(rows: list[dict[str, str]]) -> str:
    lines: dict[tuple[int, int, int, int], list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        text = row.get("text", "").strip()
        if not text:
            continue
        key = tuple(int(row.get(name) or 0) for name in ("page_num", "block_num", "par_num", "line_num"))
        lines[key].append((int(row.get("word_num") or 0), text))
    return "\n".join(" ".join(text for _, text in sorted(words)) for _, words in sorted(lines.items()))


def keyword_contexts(text: str, radius: int = 90) -> list[dict[str, object]]:
    folded = text.casefold()
    contexts: list[dict[str, object]] = []
    for term in TERMS:
        start = 0
        needle = term.casefold()
        while True:
            index = folded.find(needle, start)
            if index < 0:
                break
            contexts.append({"term": term, "offset": index, "context": text[max(0, index-radius):index+len(term)+radius]})
            start = index + len(needle)
    return contexts


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    staging = root / "data/staging/archive_ocr/pasaxon_external_round7"
    raw_dir = staging / "raw"
    ocr_dir = staging / "ocr"
    attempt_dir = staging / "ocr_backend_attempts"
    text_dir = staging / "ocr_text"
    tsv_dir = staging / "ocr_tsv"
    for directory in (ocr_dir, attempt_dir, text_dir, tsv_dir):
        directory.mkdir(parents=True, exist_ok=True)

    executable = root / "data/tools/tesseract_env/Library/bin/tesseract.exe"
    tessdata = root / "data/tools/tessdata"
    if not executable.is_file():
        raise FileNotFoundError(executable)
    for language in ("lao", "eng"):
        if not (tessdata / f"{language}.traineddata").is_file():
            raise FileNotFoundError(tessdata / f"{language}.traineddata")

    artifacts: list[dict[str, object]] = []
    for image_path in sorted(raw_dir.glob("*.jpg")):
        stem = image_path.stem
        output_json = ocr_dir / f"{stem}.ocr.json"
        if output_json.is_file():
            previous = json.loads(output_json.read_text(encoding="utf-8"))
            if previous.get("status") == "ocr_unavailable":
                shutil.copy2(output_json, attempt_dir / f"{stem}.liteparse_unavailable.json")

        command = [
            str(executable), str(image_path), "stdout", "--tessdata-dir", str(tessdata),
            # PSM 3 returned an empty page on these low-resolution full-page
            # photographs; PSM 6 produces auditable word boxes and is retained
            # with an explicit manual-review flag below.
            "-l", "lao+eng", "--psm", "6", "tsv",
        ]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Tesseract failed for {image_path.name}: {result.stderr.decode('utf-8', errors='replace')}")
        tsv_text = result.stdout.decode("utf-8-sig", errors="replace")
        tsv_path = tsv_dir / f"{stem}.tsv"
        tsv_path.write_text(tsv_text, encoding="utf-8")
        rows = list(csv.DictReader(tsv_text.splitlines(), delimiter="\t"))
        words: list[dict[str, object]] = []
        confidence_weight = confidence_total = 0.0
        for row in rows:
            value = row.get("text", "").strip()
            try:
                confidence = float(row.get("conf") or -1)
            except ValueError:
                confidence = -1
            if not value or confidence < 0:
                continue
            weight = max(len(value), 1)
            confidence_total += confidence * weight
            confidence_weight += weight
            words.append({
                "text": value,
                "bbox": [int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"])],
                "confidence": round(confidence / 100.0, 4),
                "block_num": int(row["block_num"]),
                "paragraph_num": int(row["par_num"]),
                "line_num": int(row["line_num"]),
                "word_num": int(row["word_num"]),
            })
        text = reconstruct_text(rows)
        text_path = text_dir / f"{stem}.txt"
        text_path.write_text(text, encoding="utf-8")
        with Image.open(image_path) as image:
            width, height = image.size
        mean_confidence = confidence_total / confidence_weight / 100.0 if confidence_weight else None
        payload = {
            "source_image": str(image_path.relative_to(root)).replace("\\", "/"),
            "source_sha256": sha256(image_path),
            "status": "completed" if text else "empty_text",
            "method": "tesseract_cli_5.5.3_lao+eng_psm6",
            "liteparse_attempt": {"version": "2.0.0", "status": "native_access_violation", "windows_exit_code": -1073741819},
            "languages": ["lao", "eng"],
            "page": 1,
            "width_px": width,
            "height_px": height,
            "text": text,
            "text_file": str(text_path.relative_to(root)).replace("\\", "/"),
            "tsv_file": str(tsv_path.relative_to(root)).replace("\\", "/"),
            "word_count": len(words),
            "confidence": round(mean_confidence, 4) if mean_confidence is not None else None,
            "text_items": words,
            "keyword_contexts": keyword_contexts(text),
            "requires_manual_review": True,
            "review_reasons": ["low-resolution full-page newspaper image", "multi-column layout", "verify headline/date/China keyword contexts against pixels"],
        }
        output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifacts.append({
            "image": str(image_path.relative_to(root)).replace("\\", "/"),
            "ocr_json": str(output_json.relative_to(root)).replace("\\", "/"),
            "text_file": str(text_path.relative_to(root)).replace("\\", "/"),
            "tsv_file": str(tsv_path.relative_to(root)).replace("\\", "/"),
            "word_count": len(words),
            "confidence": payload["confidence"],
            "keyword_context_count": len(payload["keyword_contexts"]),
            "ocr_json_sha256": sha256(output_json),
            "text_sha256": sha256(text_path),
            "tsv_sha256": sha256(tsv_path),
        })

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "network_used": False,
        "canonical_database_modified": False,
        "method": "Tesseract CLI fallback after LiteParse 2.0 Windows native access violation",
        "tesseract_executable": str(executable.relative_to(root)).replace("\\", "/"),
        "models": {
            "lao": {"file": "data/tools/tessdata/lao.traineddata", "sha256": sha256(tessdata / "lao.traineddata")},
            "eng": {"file": "data/tools/tessdata/eng.traineddata", "sha256": sha256(tessdata / "eng.traineddata")},
        },
        "artifacts": artifacts,
    }
    manifest_path = staging / "ocr_runtime_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Keep the batch-level manifest authoritative after the OCR fallback.
    batch_manifest_path = staging / "manifest.json"
    batch_manifest = json.loads(batch_manifest_path.read_text(encoding="utf-8"))
    batch_manifest["ocr"] = {
        "mode": "local",
        "requested_language": "lao+eng",
        "requested_dpi": 300,
        "status": "completed_with_tesseract_fallback",
        "liteparse": {
            "version": "2.0.0",
            "status": "native_access_violation",
            "windows_exit_code": -1073741819,
        },
        "fallback": {
            "engine": "Tesseract CLI 5.5.3",
            "page_segmentation_mode": 6,
            "models": manifest["models"],
        },
        "artifacts": artifacts,
    }
    batch_manifest["counts"].update({
        "ocr_images_attempted": len(artifacts),
        "ocr_complete": sum(1 for x in artifacts if int(x["word_count"]) > 0),
        "ocr_unavailable": 0,
        "ocr_artifacts": len(artifacts),
        "ocr_words": sum(int(x["word_count"]) for x in artifacts),
    })
    documentation = []
    for name in ("ocr_audit.md", "README.md"):
        path = staging / name
        documentation.append({"file": name, "bytes": path.stat().st_size, "sha256": sha256(path)})
    batch_manifest["documentation"] = documentation
    batch_manifest_path.write_text(json.dumps(batch_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"images": len(artifacts), "words": sum(int(x["word_count"]) for x in artifacts), "manifest": str(manifest_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
