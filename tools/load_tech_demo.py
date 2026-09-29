"""Select 200 distinct technology PDFs and build a local, unreviewed demo."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader
import build_design_preview as preview

CORPUS = preview.ROOT / "resume/resume-real-pdfs-2480"
ROLE = re.compile(r"\b(?:information technology|software|developer|programmer|systems? engineer|network|cyber|database|data scientist|data analyst|business systems|web designer|quality assurance|test engineer|technical support|computer|electronics|electrical|automation|engineering|IT)\b", re.I)
SKILL = re.compile(r"\b(?:python|java|javascript|sql|linux|windows|cisco|aws|azure|active directory|html|css|vmware|autocad|solidworks|matlab|plc|embedded|tcp/ip|oracle|sap|help desk|troubleshooting|hardware|networking|servers?)\b", re.I)


def main() -> None:
    csv.field_size_limit(10_000_000)
    with (CORPUS / "Resume/Resume.csv").open(encoding="utf-8-sig", newline="") as source:
        records = list(csv.DictReader(source))
    records.sort(key=lambda row: (row["Category"] != "INFORMATION-TECHNOLOGY", row["ID"]))
    candidates = []
    hashes = set()
    for record in records:
        if not ROLE.search(record["Resume_str"][:160]):
            continue
        path = CORPUS / "data/data" / record["Category"] / (record["ID"] + ".pdf")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in hashes:
            continue
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0] if lines else ""
        terms = sorted({match.group().lower() for match in SKILL.finditer(text)})
        if not ROLE.search(title) or not terms:
            continue
        if re.search(r"quality assurance", title, re.I) and not (
            re.search(r"software", title, re.I) or
            re.search(r"\b(?:python|java|javascript|sql|linux|oracle|selenium|test automation)\b", text, re.I)
        ):
            continue
        # General engineering needs multiple technology signals; explicitly IT
        # and software titles can qualify with one technical term.
        if re.search(r"engineering", title, re.I) and len(terms) < 2:
            continue
        candidates.append((record, path, content, digest, title, terms, len(reader.pages)))
        hashes.add(digest)
        if len(candidates) == 200:
            break
    if len(candidates) != 200:
        raise RuntimeError(f"Only {len(candidates)} technology PDFs matched; no demo changed")
    data = preview.payload()
    documents = []
    manifest = []
    destination = preview.OUTPUT.parent / "real-tech-resumes"
    destination.mkdir(parents=True, exist_ok=True)
    for number, (record, path, content, digest, title, terms, pages) in enumerate(candidates, 1):
        filename = record["ID"] + ".pdf"
        target = destination / filename
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"Refusing to overwrite different original: {filename}")
        else:
            with target.open("xb") as output:
                output.write(content)
        row = preview._document(number, review="unreviewed", processing="ready", summary=None)
        row.update(document_id="corpus-" + record["ID"], display_name=f"{title[:95]} · {record['ID']}",
                   original_filename=filename, current_rel_path="real-tech-resumes/" + filename,
                   document_link="./real-tech-resumes/" + filename, size_bytes=len(content),
                   submitted_at=None, ingested_at=datetime.now(timezone.utc).isoformat(),
                   summary_text=f"Local extraction: {title}. Technical terms found: {', '.join(terms[:10])}. No model assessment.",
                   processing_detail=f"Extracted locally from {pages} PDF pages. Model analysis has not run.")
        documents.append(row)
        manifest.append(dict(document_id=row["document_id"], source=str(path.relative_to(preview.ROOT)),
                             sha256=digest, category=record["Category"], extracted_role=title,
                             technical_terms=terms, pages=pages))
    data["documents"] = documents
    data["counts"] = dict(total=200, filtered=200, processed=200, unreviewed=200,
                          keep=0, reject=0, hold=0, manual_review=0, pending_action=0,
                          needs_recheck=0, open_tasks=0)
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["instance"].update(instance_id="inst_real_tech_demo", job_title="Technology roles — corpus review",
                             last_analysis_at=None, state_revision=0, criteria=[], criteria_version=0)
    for row in documents:
        row["criteria"] = []
    (preview.OUTPUT.parent / "real-tech-selection.json").write_text(
        json.dumps(dict(selection="Role title plus technical terms; no suitability ranking",
                        analysis="Local PDF extraction only; no model run", count=200,
                        categories=dict(Counter(item["category"] for item in manifest)),
                        documents=manifest), indent=2), encoding="utf-8")
    preview.build(data, real_data=True)
    print(dict(Counter(item["category"] for item in manifest)))


if __name__ == "__main__":
    main()
