# Enhanced Cortex Search: Long-Contract Retrieval Evaluation

A self-contained Snowflake Workspace notebook that **measures** how different retrieval strategies affect
answer accuracy when you use [Cortex Search](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-overview)
for question answering over **long, amended contracts**. It ships with 50 synthetic contracts (11–50 pages) and an
exact answer key, so you can reproduce the results on any Snowflake account without customer data.

---

## The problem

Retrieval-augmented generation (RAG) over long documents answers from the few chunks that score highest for a
question. Contracts often spread one answer across distant parts of the document:

| Pattern | Example |
|---|---|
| **Amendment override** | §15.3 says 60 days' notice; Amendment No. 1, many pages later, says "replace 60 with 120" without ever mentioning termination |
| **Stacked override** | The same term is amended twice; only the latest value is correct |
| **Exhibit conflict** | Exhibit C says breaches are reported in 72 hours, Exhibit D says 24 hours, and only the order-of-precedence clause says which one wins |
| **Definition chain** | "Covered Data" → includes "Restricted" data → defined in Exhibit C → includes employee compensation data |
| **Exception elsewhere** | "Service credits are the sole remedy" except for a Chronic SLA Failure, defined in an exhibit |
| **Synonyms** | The user asks about "rate increases" or "walking away early"; the contract says "increase the Fees" or "terminate for convenience" |

A top-k chunk retriever often returns the obvious clause and misses the clause that overrides or qualifies it.
That produces confident, wrong answers.

## What the notebook tests

Each configuration builds on the previous one, so the score differences show what each strategy adds.
Every configuration filters retrieval to the contract being asked about (`CONTRACT_ID`), as a real app would.

| Config | Strategy | Approach |
|---|---|---|
| **A** | Baseline | Fixed 1,800-character chunks, no metadata, top-5 retrieval |
| **B** | Strategy 1: section-aware chunks + contextual headers | Re-tags headings so `Section N.`, `EXHIBIT X` and `AMENDMENT NO. N` are detected reliably (the parser's heading levels vary), splits by section within the 512-token embedding window, and prefixes every chunk with contract title, parties, effective date, part and section. Amendment chunks are flagged. |
| **C** | Strategy 2: document digest rows | Once per contract, at ingest, an LLM reads the **full** text and writes three digests: *current terms after all amendments*, *defined terms and cross-references*, and *rights, remedies and termination*. Each digest is split by topic and indexed next to the clauses. |
| **D** | Strategy 3: orchestrated retrieval | Several filtered searches per question: top 15 clauses, top 3 digest pieces, top 3 amendment clauses, plus the neighbouring chunks of the top hits. |
| **E** | Strategy 4: query expansion | An LLM rewrites each question with contract-style synonyms. Results from the expanded query are **added to** (not swapped for) the original query's results. |

### Evaluation method
- **~538 questions** (10–11 per contract) with expected answers **computed from the generated contract terms**, so they're exact.
- **Paraphrase twin for every question.** The same intent, deliberately avoiding the contract's wording, to measure synonym robustness (~1,076 questions total).
- **LLM-as-judge.** `AI_COMPLETE` answers from the retrieved context only, then a second `AI_COMPLETE` call grades the answer against the expected answer.
- **Batched in SQL.** Answering, judging, digests and query expansion run as set-based `AI_COMPLETE` calls, so the warehouse runs them in parallel. Retrieval runs in a Python thread pool.

### Results views
Results are aggregated over every question in the run:
- **Overall** accuracy per config.
- **Breakdowns by:**
  - complexity
  - whether the answer depends on an amendment
  - contract length
  - drafting style
  - question type
  - **original vs. paraphrased wording** (the "paraphrase gap")
- **Per contract:** a table of accuracy for each contract, plus a query that lists the remaining misses with the judge's reasoning.

### Example results (10-contract run, 218 questions per config)

| Config | Original wording | Paraphrased | Paraphrase gap |
|---|---|---|---|
| A. Baseline | 86/109 | 74/109 | 11 pts |
| B. Strategy 1 | 91/109 | 72/109 | 17 pts |
| C. Strategy 2 | 97/109 | 97/109 | 0 pts |
| D. Strategy 3 | 98/109 | 100/109 | −2 pts |
| E. Strategy 4 | 98/109 | 99/109 | −1 pt |

What this run showed:
- **Digest rows (Strategy 2) gave the biggest improvement.** They also closed the synonym gap, because they restate every term in standard vocabulary.
- **Strategy 1 helps when users use the contract's own words, but not with paraphrases.**
- **Query expansion added nothing measurable once digests were in place.** The D vs. E difference is LLM-judge noise.

Your numbers will vary: the judge isn't deterministic, and a difference of a question or two is within noise.

---

## Repository layout

```
Enhanced-Cortex-Search/
├── cortex_search_multi_contract.ipynb   # the notebook (Snowflake Workspace, nbformat 4.5)
├── contracts/
│   ├── pdf/contract_01.pdf … contract_50.pdf        # loaded by the notebook
│   ├── markdown/contract_01.md … contract_50.md     # same content as text, for reading/diffing
│   ├── contracts_manifest.csv                       # type, style, complexity, amendments, pages, parties
│   └── contracts_answer_key.json                    # questions, expected answers, source sections
└── scripts/
    ├── generate_contracts.py     # regenerates the 50 contracts + manifest + answer key (seeded, reproducible)
    └── build_multi_notebook.py   # regenerates the notebook from source
```

### About the sample contracts
All contracts are **synthetic**; no real parties or customer data are involved. The set varies:
- **Type:** MSA, SaaS subscription, supply, software licence, consulting, data processing, commercial lease.
- **Drafting style:** plain English, British English (£, "Working Days", day-month dates) and formal legalese (`sixty (60)`, recitals).
- **Complexity:**
  - low: no amendments
  - medium: one amendment
  - high: two or three amendments, including stacked overrides
- **Exhibit D:** 10 healthcare/DPA contracts include an exhibit that overrides the breach-notice window.
- **Length:** 11–50 pages. Boilerplate dilutes the key terms, as in real agreements.

---

## Setup

### Prerequisites
- A Snowflake account where Cortex AI functions and Cortex Search are available (or [cross-region inference](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cross-region-inference) is enabled).
- A role with:
  - `SNOWFLAKE.CORTEX_USER`
  - `CREATE DATABASE`, or `CREATE SCHEMA` on an existing database
  - `CREATE CORTEX SEARCH SERVICE` and `CREATE STAGE` on the target schema
  - `USAGE` on a warehouse
- The LLM used for answering, judging and digests (default `claude-sonnet-4-5`) must be available in your region. Any `AI_COMPLETE` model works; set it with `LLM`.

### Option A — Git-backed workspace (recommended)
1. In Snowsight: **Projects » Workspaces » From Git repository**, and point it at this repo.
   (Creating a Git-backed workspace is done in the UI only. It needs an API integration for GitHub.)
2. Open `cortex_search_multi_contract.ipynb`. The `contracts/` folder is already in place.
3. Edit the **Parameters** cell (see below) and run all cells top to bottom.

### Option B — Upload files to a regular workspace
1. Create a workspace in Snowsight (**Projects » Workspaces**).
2. Upload `cortex_search_multi_contract.ipynb` and the whole `contracts/` folder, keeping the `contracts/pdf/` sub-folder.
   Alternatively, put the PDFs, `contracts_manifest.csv` and `contracts_answer_key.json` directly next to the notebook. The notebook detects either layout.
3. Edit the **Parameters** cell and run all cells.

### Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `DB`, `SCHEMA` | `CORTEX_SEARCH_DEMO`, `CONTRACTS_MULTI` | Where every object is created (created if missing) |
| `WAREHOUSE` | `COMPUTE_WH` | Parsing, chunking, AI functions and search refresh. MEDIUM or smaller is recommended. |
| `STAGE` | `CONTRACT_STAGE` | Internal stage for the PDFs (created with SSE encryption and a directory table) |
| `LLM` | `claude-sonnet-4-5` | Model for answering, judging, digests and query expansion |
| `CONTRACT_LIMIT` | `None` | For example `10` for a quick run. `None` uses all 50. |
| `MAX_WORKERS` | `8` | Parallel search calls. Set to `1` if you see concurrency errors. |
| `INCLUDE_PARAPHRASES` | `True` | Adds the synonym/paraphrase twin for every question |
| `DATA_DIR` | `contracts` | Location of the manifest/answer key; falls back to the notebook's folder |

### Runtime and cost
Approximate figures for all 50 contracts with paraphrases:

| Step | Volume |
|---|---|
| `AI_PARSE_DOCUMENT` | ~1,500 pages (LAYOUT mode) |
| Cortex Search services | 3, each a few minutes to build |
| Digests | 150 long-context `AI_COMPLETE` calls |
| Evaluation | ~1,076 questions × 5 configs × 2 `AI_COMPLETE` calls, plus ~1,076 query-expansion calls |

**Start with `CONTRACT_LIMIT = 10`.** That's about a fifth of the cost and is enough to see the trends.

### Cleanup
Search services incur serving cost while they exist. Run the last cell (uncomment it) or:
```sql
DROP CORTEX SEARCH SERVICE IF EXISTS CORTEX_SEARCH_DEMO.CONTRACTS_MULTI.CS_BASELINE;
DROP CORTEX SEARCH SERVICE IF EXISTS CORTEX_SEARCH_DEMO.CONTRACTS_MULTI.CS_CONTEXTUAL;
DROP CORTEX SEARCH SERVICE IF EXISTS CORTEX_SEARCH_DEMO.CONTRACTS_MULTI.CS_ENRICHED;
DROP SCHEMA IF EXISTS CORTEX_SEARCH_DEMO.CONTRACTS_MULTI;
```

---

## Objects created

| Object | Purpose |
|---|---|
| `CONTRACT_STAGE` | PDFs |
| `CONTRACTS_META` | Contract metadata (stands in for a contract-management system) |
| `EVAL_QUESTIONS` | Questions, expected answers, question type, phrasing |
| `PARSED_DOCS` | `AI_PARSE_DOCUMENT` output per contract |
| `CHUNKS_BASELINE` / `CS_BASELINE` | Config A |
| `CHUNKS_CONTEXTUAL` / `CS_CONTEXTUAL` | Config B |
| `DIGEST_RAW`, `CHUNKS_ENRICHED` / `CS_ENRICHED` | Configs C, D, E |
| `QUERY_EXPANSIONS` | Config E expanded queries |
| `EVAL_CONTEXTS`, `EVAL_RESULTS` | Retrieved context, answers, verdicts per config |

---

## Regenerating the data or the notebook

```bash
# 50 contracts + manifest + answer key -> contracts/   (seeded: same output every run)
uv run --with reportlab --with pypdf python scripts/generate_contracts.py

# notebook -> ./cortex_search_multi_contract.ipynb
python3 scripts/build_multi_notebook.py
```
Edit `N_CONTRACTS`, the term ranges or the drafting phrasing in `generate_contracts.py` to create harder or different test sets.

---

## Implementation notes and known issues

- **`AI_COMPLETE` returns a VARIANT.** Every call is cast with `::STRING`. Without the cast, verdicts come back JSON-quoted and every answer is graded INCORRECT.
- **Heading levels from `AI_PARSE_DOCUMENT` vary** (`#`, `##` or plain text for the same kind of heading). Config B re-tags them with a regex before splitting. The `structure_check` cell compares detected amendments with the manifest.
- **Digest pieces stay within the embedding window.** Digests are split by their own headings into pieces of ≤1,500 characters, so the 512-token embedding window covers each piece.
- **Query expansion is added to retrieval, never substituted for it.** An expanded query alone can let synonym-heavy boilerplate crowd the top results.
- **The LLM judge is somewhat noisy** (for example, "10% of fees" vs. "10% = $2,500", or "July 1" vs. "end of June"). Compare trends and per-question views, not one exact score.
- **`SEARCH_PREVIEW` is used for convenience in a notebook.** In applications, use the Cortex Search REST or Python API.

## Caveats
This is synthetic data built to isolate specific retrieval failures. Real contracts have messier layouts, scanned
pages and question types not covered here. Use this notebook to explain **why** each strategy helps, then re-run the
same evaluation harness on customer-approved documents with their real questions.

## References
- [Cortex Search overview](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-overview) (chunking, embedding models, multi-index)
- [Customizing Cortex Search scoring](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-customize-scoring)
- [AI_PARSE_DOCUMENT](https://docs.snowflake.com/en/user-guide/snowflake-cortex/parse-document)
- [Use Cortex Search with Cortex Agents / analytical search](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-agents)
- [Impact of retrieval chunking on finance RAG](https://www.snowflake.com/en/engineering-blog/impact-retrieval-chunking-finance-rag/) (Snowflake engineering blog)
