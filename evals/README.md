# MediX evaluation workspace

`MediX MiniBench v0.1` is a small, frozen corpus for retrieval and evidence-chain evaluation.
It deliberately avoids downloading full PubMed, MedCorp, PMC, or DailyMed releases.

## Build the 45-document corpus

```bash
conda run -n medix-swarm python scripts/build_minibench_corpus.py --register
```

The command is resumable. Files are stored under `data/uploads/minibench-v0.1/`,
registered in SQLite with status `uploaded`, and remain unparsed/unindexed until a
user explicitly starts parsing in the knowledge-base UI.

The generated `manifest.json` records source URL, publisher, license, external ID,
download time, byte size, and SHA-256. Downloaded corpus files are intentionally
gitignored; the manifest remains next to the local files so it describes the exact
frozen snapshot.

## Composition

- 5 WHO publications (`clinical_guideline`, high authority)
- 5 China NHC publications (`clinical_guideline`, high authority)
- 12 FDA/openFDA drug labels (`drug_label`, high authority)
- 23 PMC Open Access systematic reviews (`research`, medium authority)

NHC documents are public government downloads, but their pages do not state a
general open-content license. Keep them local unless redistribution permission has
been verified. PMC article licenses are checked per article and saved in the manifest.

## Verify without network access

```bash
conda run -n medix-swarm python scripts/build_minibench_corpus.py --verify-only
```

Verification checks every file against its recorded SHA-256, detects duplicate
content, and enforces the corpus size cap.
