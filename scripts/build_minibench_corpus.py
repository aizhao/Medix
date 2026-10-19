#!/usr/bin/env python3
"""Build a small, frozen, provenance-aware medical RAG corpus.

The default plan contains 45 documents:
  * 5 WHO publications (PDF)
  * 5 Chinese NHC publications (PDF)
  * 12 current FDA drug-label records (normalized TXT)
  * 23 PMC Open Access systematic reviews (normalized TXT)

Downloads are resumable, capped by file/total size, hashed, and recorded in a
manifest. With ``--register`` the files are added to MediX as ``uploaded`` so
parsing and vector indexing remain explicit actions in the knowledge UI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "uploads" / "minibench-v0.1"
CORPUS_ID = "medix-minibench-v0.1"
TARGET_DOCUMENTS = 45
MIN_DOCUMENTS = 40
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 120 * 1024 * 1024
USER_AGENT = "MediX-MiniBench/0.1 (local research corpus; contact=local)"


DIRECT_PDFS: List[Dict[str, str]] = [
    {
        "id": "who-hypertension-pharmacological-guideline-2021",
        "title": "WHO guideline for the pharmacological treatment of hypertension in adults",
        "url": "https://iris.who.int/bitstream/handle/10665/344424/9789240033986-eng.pdf",
        "source_url": "https://iris.who.int/handle/10665/344424",
        "publisher": "World Health Organization",
        "license": "CC BY-NC-SA 3.0 IGO",
        "language": "en",
        "published_at": "2021",
        "filename": "who_hypertension_pharmacological_guideline_2021.pdf",
    },
    {
        "id": "who-hypertension-evidence-annex-a-2021",
        "title": "WHO hypertension guideline web annex A: summary of evidence",
        "url": "https://iris.who.int/bitstream/handle/10665/344384/9789240033993-eng.pdf",
        "source_url": "https://iris.who.int/handle/10665/344384",
        "publisher": "World Health Organization",
        "license": "CC BY-NC-SA 3.0 IGO",
        "language": "en",
        "published_at": "2021",
        "filename": "who_hypertension_evidence_annex_a_2021.pdf",
    },
    {
        "id": "who-hypertension-decision-annex-b-2021",
        "title": "WHO hypertension guideline web annex B: evidence-to-decision frameworks",
        "url": "https://iris.who.int/bitstream/handle/10665/344385/9789240034006-eng.pdf",
        "source_url": "https://iris.who.int/handle/10665/344385",
        "publisher": "World Health Organization",
        "license": "CC BY-NC-SA 3.0 IGO",
        "language": "en",
        "published_at": "2021",
        "filename": "who_hypertension_decision_annex_b_2021.pdf",
    },
    {
        "id": "who-pen-primary-care-2020",
        "title": "WHO package of essential noncommunicable disease interventions for primary health care",
        "url": "https://iris.who.int/bitstream/handle/10665/334186/9789240009226-eng.pdf",
        "source_url": "https://iris.who.int/handle/10665/334186",
        "publisher": "World Health Organization",
        "license": "CC BY-NC-SA 3.0 IGO",
        "language": "en",
        "published_at": "2020",
        "filename": "who_pen_primary_care_2020.pdf",
    },
    {
        "id": "who-global-hypertension-report-2025",
        "title": "Global report on hypertension 2025: high stakes, turning evidence into action",
        "url": "https://iris.who.int/bitstream/handle/10665/382841/9789240115569-eng.pdf",
        "source_url": "https://iris.who.int/handle/10665/382841",
        "publisher": "World Health Organization",
        "license": "CC BY-NC-SA 3.0 IGO",
        "language": "en",
        "published_at": "2025",
        "filename": "who_global_hypertension_report_2025.pdf",
    },
    {
        "id": "nhc-hypertension-diet-guide-2023",
        "title": "成人高血压食养指南（2023年版）",
        "url": "https://www.nhc.gov.cn/cms-search/downFiles/63f752a17cfd4b4781f744477561866f.pdf",
        "publisher": "中华人民共和国国家卫生健康委员会",
        "license": "政府网站公开资料；仅用于本地研究评测，重新分发前需核对许可",
        "language": "zh-CN",
        "published_at": "2023",
        "filename": "nhc_hypertension_diet_guide_2023.pdf",
    },
    {
        "id": "nhc-hypertension-diet-guide-faq-2023",
        "title": "《成人高血压食养指南（2023年版）》问答",
        "url": "https://www.nhc.gov.cn/wjw/c100378/202301/7faed9993c9b45aea158c5335346f282/files/1732844468136_16321.pdf",
        "publisher": "中华人民共和国国家卫生健康委员会",
        "license": "政府网站公开资料；仅用于本地研究评测，重新分发前需核对许可",
        "language": "zh-CN",
        "published_at": "2023",
        "filename": "nhc_hypertension_diet_guide_faq_2023.pdf",
    },
    {
        "id": "nhc-diabetes-diet-guide-faq-2023",
        "title": "《成人糖尿病食养指南（2023年版）》问答",
        "url": "https://www.nhc.gov.cn/wjw/c100378/202301/7faed9993c9b45aea158c5335346f282/files/1732844468167_10535.pdf",
        "publisher": "中华人民共和国国家卫生健康委员会",
        "license": "政府网站公开资料；仅用于本地研究评测，重新分发前需核对许可",
        "language": "zh-CN",
        "published_at": "2023",
        "filename": "nhc_diabetes_diet_guide_faq_2023.pdf",
    },
    {
        "id": "nhc-hyperlipidemia-diet-guide-2023",
        "title": "成人高脂血症食养指南（2023年版）",
        "url": "https://www.nhc.gov.cn/cms-search/downFiles/252e7bd69d064fc5a00f02fbc64b5e23.pdf",
        "publisher": "中华人民共和国国家卫生健康委员会",
        "license": "政府网站公开资料；仅用于本地研究评测，重新分发前需核对许可",
        "language": "zh-CN",
        "published_at": "2023",
        "filename": "nhc_hyperlipidemia_diet_guide_2023.pdf",
    },
    {
        "id": "nhc-hyperlipidemia-diet-guide-faq-2023",
        "title": "《成人高脂血症食养指南（2023年版）》问答",
        "url": "https://www.nhc.gov.cn/cms-search/downFiles/cd496bd490bc4564beeb009c1612eb11.pdf",
        "publisher": "中华人民共和国国家卫生健康委员会",
        "license": "政府网站公开资料；仅用于本地研究评测，重新分发前需核对许可",
        "language": "zh-CN",
        "published_at": "2023",
        "filename": "nhc_hyperlipidemia_diet_guide_faq_2023.pdf",
    },
]

FDA_DRUGS = [
    "metformin", "amlodipine", "losartan", "lisinopril",
    "hydrochlorothiazide", "atorvastatin", "empagliflozin", "dapagliflozin",
    "semaglutide", "insulin glargine", "carvedilol", "spironolactone",
]

PMC_PLANS = [
    ("hypertension", 'hypertension[Title] AND ("systematic review"[Title] OR "meta-analysis"[Title]) AND open access[filter] AND 2020:2026[Publication Date]', 8),
    ("type2_diabetes", '("type 2 diabetes"[Title] OR T2DM[Title]) AND ("systematic review"[Title] OR "meta-analysis"[Title]) AND open access[filter] AND 2020:2026[Publication Date]', 8),
    ("cardiometabolic_drugs", '(antihypertensive[Title] OR metformin[Title] OR insulin[Title] OR semaglutide[Title] OR SGLT2[Title]) AND ("systematic review"[Title] OR "meta-analysis"[Title]) AND open access[filter] AND 2020:2026[Publication Date]', 7),
]

FDA_TEXT_FIELDS = [
    "boxed_warning", "indications_and_usage", "dosage_and_administration",
    "contraindications", "warnings_and_cautions", "warnings", "adverse_reactions",
    "drug_interactions", "use_in_specific_populations", "clinical_pharmacology",
    "how_supplied", "patient_medication_information",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:80]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _request(url: str, *, max_bytes: int = MAX_FILE_BYTES, retries: int = 3) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": "https://www.nhc.gov.cn/" if "nhc.gov.cn" in url else url,
    }
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > max_bytes:
                    raise ValueError(f"remote file exceeds {max_bytes} bytes")
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"download exceeds {max_bytes} bytes")
                return data
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}: {last_error}")


def _write_file(path: Path, data: bytes) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"filename": path.name, "size_bytes": len(data), "sha256": _sha256(data)}


def _who_pdf_from_repository(handle_url: str, preferred_filename: str) -> bytes:
    """Resolve a WHO handle through the DSpace REST API to its PDF bitstream."""
    handle = handle_url.rstrip("/").split("/handle/", 1)[-1]
    item_url = "https://iris.who.int/server/api/pid/find?" + urllib.parse.urlencode({"id": handle})
    item = json.loads(_request(item_url, max_bytes=2 * 1024 * 1024).decode("utf-8"))
    bundles_url = ((item.get("_links") or {}).get("bundles") or {}).get("href")
    if not bundles_url:
        raise ValueError(f"WHO item {handle} has no bundles link")
    separator = "&" if "?" in bundles_url else "?"
    bundles = json.loads(_request(f"{bundles_url}{separator}size=50", max_bytes=2 * 1024 * 1024).decode("utf-8"))
    bundle_items = (bundles.get("_embedded") or {}).get("bundles") or []
    original = next((bundle for bundle in bundle_items if str(bundle.get("name")).upper() == "ORIGINAL"), None)
    if not original:
        raise ValueError(f"WHO item {handle} has no ORIGINAL bundle")
    bitstreams_url = ((original.get("_links") or {}).get("bitstreams") or {}).get("href")
    if not bitstreams_url:
        raise ValueError(f"WHO item {handle} has no bitstreams link")
    separator = "&" if "?" in bitstreams_url else "?"
    bitstreams = json.loads(_request(f"{bitstreams_url}{separator}size=50", max_bytes=2 * 1024 * 1024).decode("utf-8"))
    candidates = (bitstreams.get("_embedded") or {}).get("bitstreams") or []
    pdfs = [item for item in candidates if str(item.get("name") or "").lower().endswith(".pdf")]
    selected = next((item for item in pdfs if item.get("name") == preferred_filename), None)
    selected = selected or (pdfs[0] if pdfs else None)
    content_url = (((selected or {}).get("_links") or {}).get("content") or {}).get("href")
    if not content_url:
        raise ValueError(f"WHO item {handle} has no PDF bitstream")
    return _request(content_url)


def _load_manifest(path: Path) -> Dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "corpus_id": CORPUS_ID,
        "target_documents": TARGET_DOCUMENTS,
        "minimum_documents": MIN_DOCUMENTS,
        "created_at": _now(),
        "documents": [],
        "errors": [],
    }


def _save_manifest(path: Path, manifest: Dict[str, Any]) -> None:
    manifest["updated_at"] = _now()
    manifest["downloaded_documents"] = len(manifest["documents"])
    manifest["total_bytes"] = sum(int(item.get("size_bytes") or 0) for item in manifest["documents"])
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _repair_manifest(output: Path, manifest_path: Path, manifest: Dict[str, Any]) -> None:
    """Remove incomplete legacy records produced by an interrupted/older run."""
    kept: List[Dict[str, Any]] = []
    changed = False
    for entry in manifest.get("documents", []):
        entry.setdefault("source_url", entry.get("url", ""))
        invalid_pmc = entry.get("source_family") == "PMC_OA" and not entry.get("external_id")
        generic_title = str(entry.get("title") or "").split(" / ", 1)[0].upper()
        invalid_fda = entry.get("source_family") == "FDA" and " AND " in generic_title
        if invalid_pmc or invalid_fda:
            candidate = output / str(entry.get("filename") or "")
            if candidate.is_file() and candidate.parent == output:
                candidate.unlink()
            manifest.setdefault("errors", []).append({
                "id": entry.get("id", "pmc-invalid"),
                "error": (
                    "removed incomplete PMC record without an external identifier"
                    if invalid_pmc else "removed multi-ingredient FDA label selected by a legacy fuzzy query"
                ),
                "at": _now(),
                "resolved_at": _now(),
            })
            changed = True
            continue
        kept.append(entry)
    if changed:
        manifest["documents"] = kept
    if changed or manifest_path.exists():
        _save_manifest(manifest_path, manifest)


def _existing_ids(manifest: Dict[str, Any]) -> set[str]:
    return {str(item.get("id")) for item in manifest.get("documents", [])}


def _add_document(manifest_path: Path, manifest: Dict[str, Any], entry: Dict[str, Any]) -> None:
    if entry["id"] in _existing_ids(manifest):
        return
    # Direct-download records use ``url`` in the static source plan; expose one
    # canonical field to the persistence and citation layers.
    entry.setdefault("source_url", entry.get("url", ""))
    projected_size = sum(int(item.get("size_bytes") or 0) for item in manifest["documents"])
    projected_size += int(entry.get("size_bytes") or 0)
    if projected_size > MAX_TOTAL_BYTES:
        raise ValueError(f"adding {entry['filename']} would exceed the corpus size cap")
    manifest["documents"].append(entry)
    for error in manifest.get("errors", []):
        if error.get("id") == entry["id"] and not error.get("resolved_at"):
            error["resolved_at"] = _now()
    _save_manifest(manifest_path, manifest)


def download_direct_pdfs(output: Path, manifest_path: Path, manifest: Dict[str, Any]) -> None:
    for source in DIRECT_PDFS:
        if source["id"] in _existing_ids(manifest):
            continue
        try:
            data = (
                _who_pdf_from_repository(source["source_url"], source["filename"])
                if source["publisher"].startswith("World") else _request(source["url"])
            )
            if not data.startswith(b"%PDF-"):
                raise ValueError("response is not a PDF")
            file_data = _write_file(output / source["filename"], data)
            family = "WHO" if source["publisher"].startswith("World") else "NHC"
            _add_document(manifest_path, manifest, {
                **source,
                **file_data,
                "source_family": family,
                "category": "clinical_guideline",
                "source_type": "public_external",
                "authority_level": "high",
                "downloaded_at": _now(),
            })
            print(f"[pdf] {source['id']} ({len(data) / 1024:.1f} KiB)")
        except Exception as exc:
            manifest["errors"].append({"id": source["id"], "error": str(exc), "at": _now()})
            _save_manifest(manifest_path, manifest)
            print(f"[warn] {source['id']}: {exc}", file=sys.stderr)


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _openfda_to_text(drug: str, result: Dict[str, Any]) -> tuple[str, Dict[str, str]]:
    openfda = result.get("openfda") or {}
    generic = _first(openfda.get("generic_name")) or drug
    brand = _first(openfda.get("brand_name"))
    manufacturer = _first(openfda.get("manufacturer_name"))
    title = " / ".join(item for item in (generic, brand) if item)
    lines = [
        f"FDA DRUG LABEL: {title}",
        f"Generic name: {generic}",
        f"Brand name: {brand}",
        f"Manufacturer: {manufacturer}",
        f"Effective time: {result.get('effective_time', '')}",
        f"SPL set ID: {_first(openfda.get('spl_set_id'))}",
    ]
    for field in FDA_TEXT_FIELDS:
        values = result.get(field)
        if not values:
            continue
        heading = field.replace("_", " ").upper()
        text = "\n".join(str(item) for item in values) if isinstance(values, list) else str(values)
        lines.extend(["", heading, text])
    return "\n".join(lines).strip() + "\n", {
        "title": title,
        "external_id": _first(openfda.get("spl_set_id")) or str(result.get("id") or drug),
        "published_at": str(result.get("effective_time") or "")[:8],
        "manufacturer": manufacturer,
    }


def _select_fda_result(drug: str, results: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Prefer the shortest single-ingredient generic label matching the query."""
    target_tokens = set(re.findall(r"[a-z0-9]+", drug.lower()))
    candidates: List[tuple[int, Dict[str, Any]]] = []
    for result in results:
        generic = _first((result.get("openfda") or {}).get("generic_name"))
        normalized = generic.lower()
        generic_tokens = set(re.findall(r"[a-z0-9]+", normalized))
        if not target_tokens.issubset(generic_tokens):
            continue
        if re.search(r"\b(and|with)\b|[/;+]", normalized):
            continue
        candidates.append((len(generic), result))
    if not candidates:
        raise ValueError(f"no single-ingredient FDA label matched {drug}")
    return min(candidates, key=lambda item: item[0])[1]


def download_fda_labels(output: Path, manifest_path: Path, manifest: Dict[str, Any]) -> None:
    for drug in FDA_DRUGS:
        identifier = f"fda-label-{_slug(drug)}"
        if identifier in _existing_ids(manifest):
            continue
        query = f'openfda.generic_name:"{drug}"'
        url = "https://api.fda.gov/drug/label.json?" + urllib.parse.urlencode({"search": query, "limit": 25})
        try:
            payload = json.loads(_request(url, max_bytes=8 * 1024 * 1024).decode("utf-8"))
            result = _select_fda_result(drug, payload["results"])
            text, details = _openfda_to_text(drug, result)
            if len(text) < 500:
                raise ValueError("label contains too little text")
            file_data = _write_file(output / f"{identifier}.txt", text.encode("utf-8"))
            _add_document(manifest_path, manifest, {
                "id": identifier,
                **details,
                **file_data,
                "source_url": url,
                "publisher": "U.S. Food and Drug Administration / openFDA",
                "license": "openFDA terms; source label rights may vary",
                "language": "en",
                "source_family": "FDA",
                "category": "drug_label",
                "source_type": "public_external",
                "authority_level": "high",
                "downloaded_at": _now(),
            })
            print(f"[fda] {drug} ({len(text) / 1024:.1f} KiB)")
        except Exception as exc:
            manifest["errors"].append({"id": identifier, "error": str(exc), "at": _now()})
            _save_manifest(manifest_path, manifest)
            print(f"[warn] {identifier}: {exc}", file=sys.stderr)


def _xml_text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _pmc_to_text(xml_data: bytes, fallback_pmcid: str = "") -> tuple[str, Dict[str, str]]:
    root = ET.fromstring(xml_data)
    article = root.find(".//article") if root.tag != "article" else root
    if article is None:
        raise ValueError("PMC response has no article")
    title = _xml_text(article.find(".//article-title"))
    journal = _xml_text(article.find(".//journal-title"))
    pmcid = ""
    for node in article.findall(".//article-id"):
        if node.attrib.get("pub-id-type") in {"pmc", "pmcid"}:
            pmcid = _xml_text(node)
            break
    pmcid = pmcid or fallback_pmcid
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    if not pmcid:
        raise ValueError("article has no PMC identifier")
    year = _xml_text(article.find(".//pub-date/year"))
    license_node = article.find(".//license")
    license_text = _xml_text(license_node)
    license_url = ""
    if license_node is not None:
        license_url = next((value for key, value in license_node.attrib.items() if key.endswith("href")), "")
        if not license_url:
            link = license_node.find(".//*[@{http://www.w3.org/1999/xlink}href]")
            if link is not None:
                license_url = link.attrib.get("{http://www.w3.org/1999/xlink}href", "")
    license_value = " ".join(item for item in (license_text[:500], license_url) if item)
    if "creative commons" not in license_value.lower() and "creativecommons.org" not in license_value.lower():
        raise ValueError("article has no explicit Creative Commons license")
    paragraphs: List[str] = []
    for node in article.findall(".//abstract//p") + article.findall(".//body//p"):
        value = _xml_text(node)
        if value and value not in paragraphs:
            paragraphs.append(value)
    text = "\n".join([
        f"TITLE: {title}", f"JOURNAL: {journal}", f"PMCID: {pmcid}",
        f"PUBLICATION YEAR: {year}", f"LICENSE: {license_value}", "", *paragraphs,
    ]).strip() + "\n"
    if len(text) < 1500:
        raise ValueError("article contains too little reusable text")
    return text, {
        "title": title or pmcid,
        "external_id": pmcid,
        "published_at": year,
        "publisher": journal or "PubMed Central",
        "license": license_value,
    }


def _pmc_candidates(query: str, limit: int) -> List[str]:
    params = {"db": "pmc", "term": query, "retmode": "json", "retmax": limit, "sort": "relevance"}
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
    payload = json.loads(_request(url, max_bytes=2 * 1024 * 1024).decode("utf-8"))
    return [str(item) for item in payload["esearchresult"]["idlist"]]


def download_pmc_articles(output: Path, manifest_path: Path, manifest: Dict[str, Any]) -> None:
    existing_external = {str(item.get("external_id")) for item in manifest.get("documents", [])}
    for topic, query, quota in PMC_PLANS:
        current = sum(1 for item in manifest["documents"] if item.get("pmc_topic") == topic)
        needed = max(0, quota - current)
        if not needed:
            continue
        try:
            candidates = _pmc_candidates(query, max(needed * 5, 20))
        except Exception as exc:
            manifest["errors"].append({"id": f"pmc-search-{topic}", "error": str(exc), "at": _now()})
            _save_manifest(manifest_path, manifest)
            continue
        for raw_id in candidates:
            if needed <= 0:
                break
            pmcid = raw_id if raw_id.upper().startswith("PMC") else f"PMC{raw_id}"
            if pmcid in existing_external:
                continue
            params = {"db": "pmc", "id": raw_id, "retmode": "xml"}
            api_key = os.getenv("NCBI_API_KEY")
            if api_key:
                params["api_key"] = api_key
            url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(params)
            try:
                xml_data = _request(url, max_bytes=MAX_FILE_BYTES)
                text, details = _pmc_to_text(xml_data, pmcid)
                identifier = f"pmc-{_slug(details['external_id'])}"
                file_data = _write_file(output / f"{identifier}.txt", text.encode("utf-8"))
                _add_document(manifest_path, manifest, {
                    "id": identifier,
                    **details,
                    **file_data,
                    "source_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{details['external_id']}/",
                    "language": "en",
                    "source_family": "PMC_OA",
                    "category": "research",
                    "source_type": "public_external",
                    "authority_level": "medium",
                    "pmc_topic": topic,
                    "downloaded_at": _now(),
                })
                existing_external.add(details["external_id"])
                needed -= 1
                print(f"[pmc] {details['external_id']} {details['title'][:70]}")
            except Exception as exc:
                manifest["errors"].append({"id": pmcid, "error": str(exc), "at": _now()})
                _save_manifest(manifest_path, manifest)
                print(f"[warn] {pmcid}: {exc}", file=sys.stderr)
            time.sleep(0.4 if not api_key else 0.12)


def register_documents(output: Path, manifest_path: Path, manifest: Dict[str, Any], db_path: Optional[str]) -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from backend.storage import SQLiteStore

    store = SQLiteStore(db_path)
    for entry in manifest["documents"]:
        if entry.get("registered_document_id"):
            continue
        path = (output / entry["filename"]).resolve()
        if not path.is_file():
            continue
        existing = store.find_document_by_sha256(entry["sha256"])
        if existing:
            entry["registered_document_id"] = existing["id"]
            store.update_document(
                existing["id"],
                category=entry["category"],
                source_type=entry["source_type"],
                authority_level=entry["authority_level"],
                source_url=entry.get("source_url") or entry.get("url", ""),
                publisher=entry["publisher"],
                license=entry["license"],
                external_id=entry.get("external_id") or entry["id"],
                corpus_id=CORPUS_ID,
                language=entry["language"],
                published_at=entry.get("published_at", ""),
            )
            continue
        document = store.add_document(
            entry["filename"], str(path), "uploaded",
            category=entry["category"],
            tags=[CORPUS_ID, entry["source_family"]],
            source_type=entry["source_type"],
            authority_level=entry["authority_level"],
            mime_type=mimetypes.guess_type(entry["filename"])[0] or "application/octet-stream",
            size_bytes=entry["size_bytes"],
            sha256=entry["sha256"],
            source_url=entry.get("source_url") or entry.get("url", ""),
            publisher=entry["publisher"],
            license=entry["license"],
            external_id=entry["external_id"] if "external_id" in entry else entry["id"],
            corpus_id=CORPUS_ID,
            language=entry["language"],
            published_at=entry.get("published_at", ""),
        )
        entry["registered_document_id"] = document["id"]
        _save_manifest(manifest_path, manifest)
        print(f"[registered] document_id={document['id']} {entry['filename']}")


def verify_corpus(output: Path, manifest: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    seen_hashes: set[str] = set()
    for entry in manifest["documents"]:
        path = output / entry["filename"]
        if not path.is_file():
            errors.append(f"missing file: {entry['filename']}")
            continue
        data = path.read_bytes()
        digest = _sha256(data)
        if digest != entry.get("sha256"):
            errors.append(f"checksum mismatch: {entry['filename']}")
        if digest in seen_hashes:
            errors.append(f"duplicate content: {entry['filename']}")
        seen_hashes.add(digest)
    total = sum(int(item.get("size_bytes") or 0) for item in manifest["documents"])
    if total > MAX_TOTAL_BYTES:
        errors.append(f"corpus exceeds {MAX_TOTAL_BYTES} bytes")
    return errors


def _resolve_fallback_errors(manifest: Dict[str, Any]) -> None:
    """Close candidate-fetch errors once every planned PMC topic quota is met."""
    planned = {topic: quota for topic, _query, quota in PMC_PLANS}
    actual = {
        topic: sum(1 for item in manifest.get("documents", []) if item.get("pmc_topic") == topic)
        for topic in planned
    }
    if not all(actual[topic] >= quota for topic, quota in planned.items()):
        return
    for error in manifest.get("errors", []):
        if not error.get("resolved_at") and str(error.get("id", "")).startswith("PMC"):
            error["resolved_at"] = _now()
            error["resolution"] = "topic quota fulfilled by an alternate Creative Commons article"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--register", action="store_true", help="register downloads as uploaded MediX documents")
    parser.add_argument("--db-path", help="SQLite path for --register; defaults to MEDIX_DB_PATH/data/medix.db")
    parser.add_argument("--allow-partial", action="store_true", help="return success with fewer than 40 documents")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    manifest = _load_manifest(manifest_path)
    _repair_manifest(output, manifest_path, manifest)

    if not args.verify_only:
        download_direct_pdfs(output, manifest_path, manifest)
        download_fda_labels(output, manifest_path, manifest)
        download_pmc_articles(output, manifest_path, manifest)
    _resolve_fallback_errors(manifest)
    errors = verify_corpus(output, manifest)
    manifest["verification_errors"] = errors
    manifest["frozen"] = len(manifest["documents"]) >= MIN_DOCUMENTS and not errors
    _save_manifest(manifest_path, manifest)

    if args.register:
        register_documents(output, manifest_path, manifest, args.db_path)

    count = len(manifest["documents"])
    size_mb = sum(int(item.get("size_bytes") or 0) for item in manifest["documents"]) / 1024 / 1024
    print(f"MiniBench: {count}/{TARGET_DOCUMENTS} documents, {size_mb:.2f} MiB")
    print(f"Manifest: {manifest_path}")
    if errors:
        print("Verification failed: " + "; ".join(errors), file=sys.stderr)
        return 2
    if count < MIN_DOCUMENTS and not args.allow_partial:
        print(f"Only {count} documents downloaded; minimum is {MIN_DOCUMENTS}. Re-run to resume.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
