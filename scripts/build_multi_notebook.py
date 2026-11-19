"""Builds cortex_search_multi_contract.ipynb: the Cortex Search long-document demo scaled to 50 contracts.

Reads (at notebook run time, from the workspace folder):
  contract_*.pdf, contracts_manifest.csv, contracts_answer_key.json  (from generate_contracts.py)
"""
import json
import uuid
from pathlib import Path

cells = []

def _id():
    return uuid.uuid4().hex[:8]

def md(text):
    cells.append({"cell_type": "markdown", "id": _id(), "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)})

def py(text):
    cells.append({"cell_type": "code", "id": _id(), "execution_count": None, "metadata": {},
                  "outputs": [], "source": text.strip("\n").splitlines(keepends=True)})

def sql(name, text):
    src = f"%%sql -r {name}\n" + text.strip("\n")
    cells.append({"cell_type": "code", "id": _id(), "execution_count": None,
                  "metadata": {"codeCollapsed": False, "language": "sql", "name": name, "resultVariableName": name},
                  "outputs": [], "source": src.splitlines(keepends=True)})

# ============================================================================ intro
md(r"""
# Cortex Search on Long Contracts — 50-Contract Evaluation

This scales the single-contract demo (`cortex_search_long_docs.ipynb`) to a **portfolio of 50 synthetic contracts**
so you can see *where* each strategy helps, broken down by complexity, drafting style, document length, and question type.

| Config | Strategy | Approach |
|---|---|---|
| A | Baseline | Fixed 1,800-char chunks, no metadata, top-5 retrieval |
| B | Strategy 1 | Normalized section-aware chunks + contextual header (contract, parties, part, section) |
| C | Strategy 2 | B + LLM-generated document digest rows, split by topic, indexed with the clauses |
| D | Strategy 3 | C + orchestrated retrieval (wider limit, forced digests and amendments, neighbor chunks) |
| E | Strategy 4 | D + LLM query expansion: results from a synonym-expanded query are added to the original query's results |

Every configuration filters retrieval to **the contract being asked about** (`CONTRACT_ID`), as a real app would.
The differences you see therefore come from chunking, indexing, and retrieval, not from mixing up contracts.

### Test set (from `generate_contracts.py`)
- 50 contracts of 11–50 pages: 7 contract types; plain-English, British, and legalese drafting; low/medium/high complexity.
- High-complexity contracts have 2–3 amendments, including a **stacked override** (the same term amended twice),
  plus conflicting exhibits resolved by an order-of-precedence clause.
- `contracts_answer_key.json`: 10–11 questions per contract (~520 total) with expected answers **computed from the
  generated terms**, so they are exact.
- **Paraphrase set (synonym test):** every question also gets a paraphrased twin that deliberately avoids the
  contract's wording ("walk away early" instead of "terminate for convenience", "raise prices" instead of
  "increase the Fees"). The expected answer is unchanged. `PHRASING` = `original` / `paraphrase` lets you measure
  how much accuracy each strategy loses when users don't use the contract's words.

### Setup prerequisites
- Run in a **Snowflake Workspace notebook**. Easiest: create the workspace **from the Git repository** so the
  `contracts/` folder comes with it. Otherwise upload `contracts/` (or put the PDFs, `contracts_manifest.csv`
  and `contracts_answer_key.json` next to this notebook). `DATA_DIR` handles both layouts.
- Role needs `CREATE DATABASE`/`SCHEMA` (or an existing schema), `CREATE CORTEX SEARCH SERVICE`, `SNOWFLAKE.CORTEX_USER`, and a warehouse.

### Execution and cost
Run cells top to bottom. With all 50 contracts:
- Parsing ~1,500 pages: several minutes.
- 3 search services: a few minutes each.
- Digests: 150 long-context LLM calls (3 per contract), run in parallel in SQL.
- Evaluation: ~520 questions × 4 configs. Retrieval runs in a thread pool; answering and judging run as **batched
  SQL** (`AI_COMPLETE` over a table), so the warehouse parallelises them. That is about 4,200 LLM calls in total.

To try it quickly, set `CONTRACT_LIMIT = 10` in the parameters cell. Paraphrases double the number of questions
(~1,040). Set `INCLUDE_PARAPHRASES = False` to skip them.
""")

# ============================================================================ params
md(r"""
## 0. Parameters
""")
py(r'''
# ---- Edit me ----------------------------------------------------------------
DB             = "CORTEX_SEARCH_DEMO"
SCHEMA         = "CONTRACTS_MULTI"        # separate from the single-contract demo schema
WAREHOUSE      = "COMPUTE_WH"             # MEDIUM or smaller is recommended for AI functions / search refresh
STAGE          = "CONTRACT_STAGE"
LLM            = "claude-sonnet-4-5"      # answer, judge, and digest model
CONTRACT_LIMIT = None                     # e.g. 10 for a quick run; None = all contracts found
MAX_WORKERS    = 8                        # parallel search calls; set to 1 if you hit concurrency errors
INCLUDE_PARAPHRASES = True                # add a synonym/paraphrase twin for every question
DATA_DIR       = "contracts"              # repo layout: contracts/pdf/*.pdf + manifest + answer key; falls back to "."
# -----------------------------------------------------------------------------

from snowflake.snowpark.context import get_active_session
session = get_active_session()

session.sql(f"CREATE DATABASE IF NOT EXISTS {DB}").collect()
session.sql(f"CREATE SCHEMA IF NOT EXISTS {DB}.{SCHEMA}").collect()
session.sql(f"USE SCHEMA {DB}.{SCHEMA}").collect()
session.sql(f"USE WAREHOUSE {WAREHOUSE}").collect()
print(session.sql("SELECT CURRENT_ROLE(), CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_WAREHOUSE()").collect())
''')

# ============================================================================ load
md(r"""
## 1. Load contracts, metadata, and the answer key
- PDFs go to an encrypted internal stage with a directory table (required by `AI_PARSE_DOCUMENT`).
- `CONTRACTS_META` simulates what a contract-management system knows: type, parties, and effective date.
  Strategy 1 uses it in the chunk headers.
- `EVAL_QUESTIONS` holds the questions and expected answers. `QTYPE` tags each question so results can be broken down by type.
""")
py(r'''
import glob, json, os
import pandas as pd

# Support both the repo layout (contracts/pdf/...) and a flat folder with everything next to the notebook.
if not os.path.exists(os.path.join(DATA_DIR, "contracts_manifest.csv")):
    DATA_DIR = "."
PDF_DIR = os.path.join(DATA_DIR, "pdf") if os.path.isdir(os.path.join(DATA_DIR, "pdf")) else DATA_DIR

pdfs = sorted(glob.glob(os.path.join(PDF_DIR, "contract_[0-9]*.pdf")))[: CONTRACT_LIMIT or None]
assert pdfs, f"No contract_NN.pdf files found in {PDF_DIR!r} - see the README setup steps."
ids = [os.path.splitext(os.path.basename(p))[0] for p in pdfs]
print(f"{len(pdfs)} contracts: {ids[0]} .. {ids[-1]}")

session.sql(f"""
    CREATE STAGE IF NOT EXISTS {STAGE}
      ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE') DIRECTORY = (ENABLE = TRUE)
""").collect()
for p in pdfs:
    session.file.put(p, f"@{STAGE}", auto_compress=False, overwrite=True)
session.sql(f"ALTER STAGE {STAGE} REFRESH").collect()

# Contract metadata (stands in for a CLM system)
meta = pd.read_csv(os.path.join(DATA_DIR, "contracts_manifest.csv"))
meta = meta[meta.contract_id.isin(ids)].copy()
meta["page_bucket"] = pd.cut(meta.pdf_pages, [0, 20, 30, 40, 60], labels=["10-20", "21-30", "31-40", "41-50"]).astype(str)
meta.columns = [c.upper() for c in meta.columns]
session.create_dataframe(meta).write.mode("overwrite").save_as_table("CONTRACTS_META")

# Questions + expected answers, tagged with a question type
QTYPES = [("terminate for convenience?", "notice period"), ("service credit", "service credit cap"),
          ("fee increase", "fee increase cap"), ("Initial Term end", "term end date"),
          ("termination for convenience fee", "termination fee calc"), ("cure period", "cure period"),
          ("uptime targets", "chronic SLA exception"), ("liability", "liability carve-out"),
          ("count as a breach of Covered Data", "definition chain"), ("security breach be reported", "breach notice / exhibit conflict"),
          ("Amendment conflicts", "order of precedence")]
def qtype(q):
    return next((t for k, t in QTYPES if k.lower() in q.lower()), "other")

# Paraphrase templates: same intent, deliberately avoiding the contract's own terms
# (no "terminate", "convenience", "service credit", "Initial Term", "cure", "Covered Data", ...).
import re
PARAPHRASE = {
    "notice period":          lambda q: "How much warning do we have to give if we want to walk away from the deal early without a reason?",
    "service credit cap":     lambda q: "What is the most compensation we can get in a single month when the service has outages?",
    "fee increase cap":       lambda q: "How much can the other side raise prices each year - are rate increases capped?",
    "term end date":          lambda q: "When does the original commitment period run out?",
    "termination fee calc":   lambda q: "If we exit early for no particular reason, what early-exit penalty would we owe, in money?",
    "cure period":            lambda q: "If the other side breaks the contract, how long do they get to fix it before we can walk away?",
    "chronic SLA exception":  lambda q: "Can we get out of the contract at no cost if the system keeps going down month after month?",
    "liability carve-out":    lambda q: "If our protected information is leaked, is the other side's financial exposure limited to the normal ceiling?",
    "definition chain":       lambda q: ("If " + re.search(r"involving (.+) count", q).group(1) +
                                         " is exposed in an incident, does that fall under the protected-data rules?"),
    "breach notice / exhibit conflict": lambda q: "How fast must they tell us after a hack or data leak is discovered?",
    "order of precedence":    lambda q: "If a later change document contradicts the original contract, which one wins?",
}

key = json.load(open(os.path.join(DATA_DIR, "contracts_answer_key.json")))
rows = []
for cid in ids:
    for i, q in enumerate(key[cid]["questions"]):
        base = {"CONTRACT_ID": cid, "QTYPE": qtype(q["question"]), "EXPECTED": q["expected"],
                "NEEDS_AMENDMENT": "Amendment" in q["sources"]}
        rows.append({**base, "QID": i, "PHRASING": "original", "QUESTION": q["question"]})
        if INCLUDE_PARAPHRASES and base["QTYPE"] in PARAPHRASE:
            rows.append({**base, "QID": 100 + i, "PHRASING": "paraphrase",
                         "QUESTION": PARAPHRASE[base["QTYPE"]](q["question"])})
questions = pd.DataFrame(rows)
session.create_dataframe(questions).write.mode("overwrite").save_as_table("EVAL_QUESTIONS")
print(f"{len(questions)} questions ({(questions.PHRASING == 'paraphrase').sum()} paraphrases); "
      f"{questions.NEEDS_AMENDMENT.sum()} require an amendment to answer correctly")
questions.groupby(["QTYPE", "PHRASING"]).size().unstack(fill_value=0)
''')
py(r'''
# Parse every PDF once (LAYOUT mode keeps headings). All strategies start from this text.
session.sql(f"""
    CREATE OR REPLACE TABLE PARSED_DOCS AS
    SELECT REPLACE(RELATIVE_PATH, '.pdf', '') AS CONTRACT_ID,
           AI_PARSE_DOCUMENT(TO_FILE('@{STAGE}', RELATIVE_PATH), {{'mode': 'LAYOUT'}}):content::STRING AS CONTENT
    FROM DIRECTORY(@{STAGE})
    WHERE RELATIVE_PATH ILIKE 'contract\\_%.pdf'
      AND REPLACE(RELATIVE_PATH, '.pdf', '') IN (SELECT CONTRACT_ID FROM CONTRACTS_META)
""").collect()
print(session.sql("SELECT COUNT(*) FROM PARSED_DOCS").collect()[0][0], "contracts parsed")
''')
sql("doc_profile", r"""
-- Portfolio profile: every contract is many times larger than one 512-token embedding window.
SELECT m.COMPLEXITY, m.STYLE, COUNT(*) AS CONTRACTS,
       ROUND(AVG(m.PDF_PAGES)) AS AVG_PAGES,
       ROUND(AVG(SNOWFLAKE.CORTEX.COUNT_TOKENS('snowflake-arctic-embed-m', p.CONTENT))) AS AVG_TOKENS,
       SUM(m.AMENDMENTS) AS AMENDMENTS
FROM {{DB}}.{{SCHEMA}}.PARSED_DOCS p JOIN {{DB}}.{{SCHEMA}}.CONTRACTS_META m USING (CONTRACT_ID)
GROUP BY 1, 2 ORDER BY 1, 2
""")

# ============================================================================ helpers
md(r"""
## 2. Evaluation harness
1. **Retrieve:** `run_retrieval()` calls the config's retrieval function for every question, in parallel, and writes
   the retrieved context to `EVAL_CONTEXTS`.
2. **Answer and judge in batch:** `score()` runs one SQL statement that calls `AI_COMPLETE` twice per row (answer,
   then judge) across all questions for that config, so the warehouse does the parallel work.
3. Results accumulate in `EVAL_RESULTS`, keyed by `CONFIG`.

`AI_COMPLETE` returns a VARIANT, so outputs are cast `::STRING`. Without the cast, verdicts come back JSON-quoted
and every answer is graded INCORRECT.
""")
py(r'''
from concurrent.futures import ThreadPoolExecutor

def fqn(name):
    return f"{DB}.{SCHEMA}.{name}"

def search(service, query, columns, limit=5, filt=None):
    """Cortex Search via SEARCH_PREVIEW. The payload is dollar-quoted so JSON escapes pass through untouched."""
    payload = {"query": query, "columns": columns, "limit": limit}
    if filt:
        payload["filter"] = filt
    body = json.dumps(payload).replace("$$", "$ $")   # keep the dollar-quoted literal intact (LLM-written queries)
    raw = session.sql(f"SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW('{fqn(service)}', $${body}$$)").collect()[0][0]
    return json.loads(raw)["results"]

def in_contract(cid, **eq):
    """Filter to one contract, optionally ANDed with attribute equality filters."""
    conds = [{"@eq": {"CONTRACT_ID": cid}}] + [{"@eq": {k: v}} for k, v in eq.items()]
    return conds[0] if len(conds) == 1 else {"@and": conds}

session.sql("""CREATE TABLE IF NOT EXISTS EVAL_CONTEXTS (CONFIG STRING, CONTRACT_ID STRING, QID INT, N_CTX INT, CONTEXT STRING)""").collect()
session.sql("""CREATE TABLE IF NOT EXISTS EVAL_RESULTS (CONFIG STRING, CONTRACT_ID STRING, QID INT, ANSWER STRING, VERDICT STRING, CORRECT BOOLEAN)""").collect()

def run_retrieval(config, retrieve_fn):
    """retrieve_fn(contract_id, question, qid=...) -> list[str]. Writes one context row per question."""
    qs = session.table("EVAL_QUESTIONS").select("CONTRACT_ID", "QID", "QUESTION").to_pandas()
    def one(r):
        ctx = retrieve_fn(r.CONTRACT_ID, r.QUESTION, qid=int(r.QID))
        return {"CONFIG": config, "CONTRACT_ID": r.CONTRACT_ID, "QID": int(r.QID), "N_CTX": len(ctx),
                "CONTEXT": "\n\n---\n\n".join(ctx)}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        out = list(pool.map(one, qs.itertuples(index=False)))
    session.sql("DELETE FROM EVAL_CONTEXTS WHERE CONFIG = ?", params=[config]).collect()
    session.create_dataframe(pd.DataFrame(out)).write.mode("append").save_as_table("EVAL_CONTEXTS")
    print(f"{config}: retrieved context for {len(out)} questions")

def score(config):
    session.sql("DELETE FROM EVAL_RESULTS WHERE CONFIG = ?", params=[config]).collect()
    session.sql(f"""
        INSERT INTO EVAL_RESULTS
        WITH answered AS (
            SELECT c.CONFIG, c.CONTRACT_ID, c.QID, q.QUESTION, q.EXPECTED,
                   AI_COMPLETE('{LLM}',
                     'You are a contract analyst. Answer ONLY from the excerpts below. If excerpts conflict, apply any '
                     || 'amendment or order-of-precedence language they contain; a later amendment overrides an earlier one. '
                     || 'Be concise and cite sections.\n\nEXCERPTS:\n' || c.CONTEXT || '\n\nQUESTION: ' || q.QUESTION
                   )::STRING AS ANSWER
            FROM EVAL_CONTEXTS c JOIN EVAL_QUESTIONS q USING (CONTRACT_ID, QID)
            WHERE c.CONFIG = ?
        ), judged AS (
            SELECT *, AI_COMPLETE('{LLM}',
                     'Grade the ANSWER against the EXPECTED answer. Numbers and the governing value must match; wording may differ. '
                     || 'Reply with exactly one word on the first line, CORRECT or INCORRECT, then one short sentence of reasoning.'
                     || '\n\nQUESTION: ' || QUESTION || '\nEXPECTED: ' || EXPECTED || '\nANSWER: ' || ANSWER
                   )::STRING AS VERDICT
            FROM answered
        )
        SELECT CONFIG, CONTRACT_ID, QID, ANSWER, VERDICT,
               STARTSWITH(UPPER(LTRIM(VERDICT, ' "*#\n')), 'CORRECT') AS CORRECT
        FROM judged
    """, params=[config]).collect()
    n, ok = session.sql("SELECT COUNT(*), COUNT_IF(CORRECT) FROM EVAL_RESULTS WHERE CONFIG = ?", params=[config]).collect()[0]
    print(f"{config}: {ok}/{n} correct ({100 * ok / n:.0f}%)")

def evaluate(config, retrieve_fn):
    run_retrieval(config, retrieve_fn)
    score(config)

def build_service(name, table, attributes):
    session.sql(f"""
        CREATE OR REPLACE CORTEX SEARCH SERVICE {name}
          ON SEARCH_TEXT
          ATTRIBUTES {", ".join(attributes)}
          WAREHOUSE = {WAREHOUSE}
          TARGET_LAG = '1 day'
        AS SELECT SEARCH_TEXT, {", ".join(attributes)}, CHUNK_ID::STRING AS CHUNK_ID FROM {table}
    """).collect()
    print(f"{name} ready")
''')

# ============================================================================ A baseline
md(r"""
## 3. Config A — Baseline
Fixed 1,800-character chunks, no overlap, no metadata other than `CONTRACT_ID`, top 5 results.
""")
py(r'''
session.sql("""
    CREATE OR REPLACE TABLE CHUNKS_BASELINE AS
    SELECT p.CONTRACT_ID, c.INDEX AS CHUNK_ID, c.VALUE::STRING AS SEARCH_TEXT
    FROM PARSED_DOCS p,
         LATERAL FLATTEN(SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(p.CONTENT, 'none', 1800, 0)) c
""").collect()
session.sql("ALTER TABLE CHUNKS_BASELINE SET CHANGE_TRACKING = TRUE").collect()
build_service("CS_BASELINE", "CHUNKS_BASELINE", ["CONTRACT_ID"])

def retrieve_baseline(cid, q, qid=None):
    return [h["SEARCH_TEXT"] for h in search("CS_BASELINE", q, ["SEARCH_TEXT"], limit=5, filt=in_contract(cid))]

evaluate("A", retrieve_baseline)
''')

# ============================================================================ B strategy 1
md(r"""
## 4. Config B — Strategy 1: normalized section-aware chunks + contextual headers
Two fixes over the single-contract version:
1. **Heading normalization.** `AI_PARSE_DOCUMENT` assigns heading levels inconsistently: a `Section N.` line may
   come back as `#`, `##`, or plain text. A regex re-tags every `Section N.` as `##` and every `EXHIBIT X` /
   `AMENDMENT NO. N` as `#` before splitting, so the part and section labels are reliable.
2. **Metadata-driven header.** The chunk header is built from `CONTRACTS_META` (title, parties, effective date)
   plus part and section. Amendment chunks are flagged and say they override earlier terms.
""")
py(r'''
session.sql(r"""
    CREATE OR REPLACE TABLE CHUNKS_CONTEXTUAL AS
    WITH norm AS (
        SELECT CONTRACT_ID,
               REGEXP_REPLACE(
                 REGEXP_REPLACE(CONTENT, '^[#* ]*(Section [0-9A-Za-z-]+[.][^\n*]*)[*]*', '## \\1', 1, 0, 'm'),
                 '^[#* ]*((EXHIBIT [A-Z]|AMENDMENT NO[.])[^\n*]*)[*]*', '# \\1', 1, 0, 'm') AS CONTENT
        FROM PARSED_DOCS
    ), s AS (
        SELECT n.CONTRACT_ID, c.INDEX AS CHUNK_ID, c.VALUE:chunk::STRING AS BODY,
               COALESCE(c.VALUE:headers:h1::STRING, 'Main agreement') AS PART,
               COALESCE(c.VALUE:headers:h2::STRING, '')               AS SECTION
        FROM norm n,
             LATERAL FLATTEN(SNOWFLAKE.CORTEX.SPLIT_TEXT_MARKDOWN_HEADER(
                 n.CONTENT, OBJECT_CONSTRUCT('#', 'h1', '##', 'h2'), 1200, 200)) c
    )
    SELECT s.CONTRACT_ID, s.CHUNK_ID, s.PART, s.SECTION, 'clause' AS CHUNK_TYPE,
           IFF(s.PART ILIKE 'AMENDMENT%', 'Y', 'N') AS IS_AMENDMENT,
           'Contract: ' || m.TITLE || ' between ' || m.PROVIDER || ' and ' || m.CUSTOMER
             || ' (effective ' || m.EFFECTIVE_DATE || ')'
             || '\nPart: ' || s.PART || IFF(s.SECTION <> '', '\nSection: ' || s.SECTION, '')
             || IFF(s.PART ILIKE 'AMENDMENT%', '\nNOTE: This amendment modifies and overrides earlier terms of the agreement.', '')
             || '\n\n' || s.BODY AS SEARCH_TEXT
    FROM s JOIN CONTRACTS_META m USING (CONTRACT_ID)
""").collect()
session.sql("ALTER TABLE CHUNKS_CONTEXTUAL SET CHANGE_TRACKING = TRUE").collect()
''')
sql("structure_check", r"""
-- Did normalization recover the structure? Detected amendments should match the manifest for every contract.
SELECT m.CONTRACT_ID, m.COMPLEXITY, m.AMENDMENTS AS EXPECTED_AMENDMENTS,
       COUNT(DISTINCT IFF(c.IS_AMENDMENT = 'Y', c.PART, NULL)) AS DETECTED_AMENDMENTS,
       COUNT(DISTINCT c.PART) AS PARTS, COUNT(DISTINCT c.SECTION) AS SECTIONS, COUNT(*) AS CHUNKS
FROM {{DB}}.{{SCHEMA}}.CONTRACTS_META m JOIN {{DB}}.{{SCHEMA}}.CHUNKS_CONTEXTUAL c USING (CONTRACT_ID)
GROUP BY 1, 2, 3
ORDER BY (EXPECTED_AMENDMENTS = DETECTED_AMENDMENTS), 1
""")
py(r'''
ATTRS = ["CONTRACT_ID", "PART", "SECTION", "CHUNK_TYPE", "IS_AMENDMENT"]
build_service("CS_CONTEXTUAL", "CHUNKS_CONTEXTUAL", ATTRS)

def retrieve_contextual(cid, q, qid=None):
    return [h["SEARCH_TEXT"] for h in search("CS_CONTEXTUAL", q, ["SEARCH_TEXT"], limit=5, filt=in_contract(cid))]

evaluate("B", retrieve_contextual)
''')

# ============================================================================ C strategy 2
md(r"""
## 5. Config C — Strategy 2: document digest rows
Once per contract, at ingest, an LLM reads the **full** text and writes three digests. They are generic,
portfolio-level instructions, not tuned to the test questions:
1. **Current terms after all amendments**: current vs. original value, with the source of each.
2. **Defined terms and cross-references**: each definition with its chain resolved.
3. **Rights, remedies, and termination**: every exit right, fee, cure period, and exception, as amended.

In the single-contract demo each digest was one long row, longer than the 512-token embedding window. Here each digest
is **split by its own headings** into pieces of ≤1,500 characters, and each piece gets the contract header, so the
whole digest is visible to semantic search.
""")
py(r'''
session.sql(f"""
    CREATE OR REPLACE TABLE DIGEST_RAW AS
    WITH prompts AS (
        SELECT column1 AS DIGEST_ID, column2 AS DIGEST_TITLE, column3 AS INSTRUCTION FROM VALUES
        (1, 'Current terms after all amendments',
            'List the CURRENT effective commercial and legal terms of this agreement after applying every amendment in date order and the order-of-precedence clause. Use a markdown heading per topic. For each term give the current value, the original value if changed, and the clause or amendment it comes from.'),
        (2, 'Defined terms and cross-references',
            'List every defined term in this agreement under its own markdown heading. Give its meaning, fully resolve chains of cross-references to other sections, exhibits and amendments, and state what the term includes and excludes.'),
        (3, 'Rights, remedies and termination',
            'Summarise, under markdown headings, every termination right, renewal mechanism, termination fee, cure period, exclusive or non-exclusive remedy, liability cap and carve-out in this agreement, as amended, with clause references and exceptions.')
    )
    SELECT p.CONTRACT_ID, pr.DIGEST_ID, pr.DIGEST_TITLE,
           AI_COMPLETE('{LLM}', pr.INSTRUCTION || '\n\nAGREEMENT:\n' || p.CONTENT, {{'max_tokens': 8192}})::STRING AS DIGEST
    FROM PARSED_DOCS p CROSS JOIN prompts pr
""").collect()

session.sql(r"""
    CREATE OR REPLACE TABLE CHUNKS_ENRICHED AS
    SELECT CONTRACT_ID, CHUNK_ID, PART, SECTION, CHUNK_TYPE, IS_AMENDMENT, SEARCH_TEXT FROM CHUNKS_CONTEXTUAL
    UNION ALL
    SELECT d.CONTRACT_ID, 100000 + d.DIGEST_ID * 1000 + c.INDEX AS CHUNK_ID,
           'Document digest' AS PART, d.DIGEST_TITLE AS SECTION, 'digest' AS CHUNK_TYPE, 'N' AS IS_AMENDMENT,
           'Contract: ' || m.TITLE || ' between ' || m.PROVIDER || ' and ' || m.CUSTOMER
             || '\nDigest: ' || d.DIGEST_TITLE
             || IFF(c.VALUE:headers:h2 IS NOT NULL OR c.VALUE:headers:h1 IS NOT NULL,
                    '\nTopic: ' || COALESCE(c.VALUE:headers:h2::STRING, c.VALUE:headers:h1::STRING), '')
             || '\n\n' || c.VALUE:chunk::STRING AS SEARCH_TEXT
    FROM DIGEST_RAW d JOIN CONTRACTS_META m USING (CONTRACT_ID),
         LATERAL FLATTEN(SNOWFLAKE.CORTEX.SPLIT_TEXT_MARKDOWN_HEADER(
             d.DIGEST, OBJECT_CONSTRUCT('#', 'h1', '##', 'h2'), 1500, 150)) c
""").collect()
session.sql("ALTER TABLE CHUNKS_ENRICHED SET CHANGE_TRACKING = TRUE").collect()
print(session.sql("SELECT CHUNK_TYPE, COUNT(*) FROM CHUNKS_ENRICHED GROUP BY 1").collect())
''')
py(r'''
build_service("CS_ENRICHED", "CHUNKS_ENRICHED", ATTRS)

def retrieve_enriched(cid, q, qid=None):
    return [h["SEARCH_TEXT"] for h in search("CS_ENRICHED", q, ["SEARCH_TEXT"], limit=5, filt=in_contract(cid))]

evaluate("C", retrieve_enriched)
''')

# ============================================================================ D strategy 3
md(r"""
## 6. Config D — Strategy 3: orchestrated retrieval
Same `CS_ENRICHED` index, with four searches per question, all scoped to the contract:
top 15 clauses, top 3 digest pieces, top 3 amendment clauses, plus the neighbouring chunks of the top 3 clause hits.
""")
py(r'''
def neighbors(cid, chunk_ids):
    ids = sorted({int(c) + d for c in chunk_ids for d in (-1, 1)} - {int(c) for c in chunk_ids})
    if not ids:
        return []
    rows = session.sql(
        f"SELECT SEARCH_TEXT FROM CHUNKS_ENRICHED WHERE CONTRACT_ID = ? AND CHUNK_TYPE = 'clause' "
        f"AND CHUNK_ID IN ({','.join(map(str, ids))})", params=[cid]).collect()
    return [r[0] for r in rows]

def retrieve_orchestrated(cid, q, qid=None, search_q=None):
    sq = search_q or q   # Config E passes an expanded query; the answer step still sees the user's question
    cols = ["SEARCH_TEXT", "CHUNK_ID"]
    clauses = search("CS_ENRICHED", sq, cols, limit=15, filt=in_contract(cid, CHUNK_TYPE="clause"))
    digests = search("CS_ENRICHED", sq, cols, limit=3,  filt=in_contract(cid, CHUNK_TYPE="digest"))
    amends  = search("CS_ENRICHED", sq, cols, limit=3,  filt=in_contract(cid, IS_AMENDMENT="Y"))
    ctx = [h["SEARCH_TEXT"] for h in digests + amends + clauses]
    ctx += neighbors(cid, [h["CHUNK_ID"] for h in clauses[:3]])
    return list(dict.fromkeys(ctx))

evaluate("D", retrieve_orchestrated)
''')

# ============================================================================ E strategy 4 (query expansion)
md(r"""
## 6b. Config E — Strategy 4: LLM query expansion (synonym handling)
The user's wording often differs from the contract's: "rate increases" vs. "increase the Fees" or "price adjustment".
Cortex Search's vector index and reranker already match on meaning, but a paraphrase can still lose to semantically
similar boilerplate. Query expansion closes that gap:

1. Before searching, an LLM rewrites each question into a search query: the original wording **plus** typical
   contract terminology and synonyms.
2. **Multi-query retrieval:** run Config D's retrieval with the original question, then add the top clauses found by
   the expanded query. Expansion *adds* recall and never replaces the original results. In testing, an expanded
   query alone can let synonym-heavy boilerplate ("rate card administration") crowd the top 5. Merging avoids that.
3. The answer step still sees the **user's original question**, so expansion can't change what is being asked.

Expansions are generated in **one batched SQL statement**. A production app makes one small LLM call per question
(Cortex Agents rewrite queries in a similar way).
""")
py(r'''
session.sql(f"""
    CREATE OR REPLACE TABLE QUERY_EXPANSIONS AS
    SELECT CONTRACT_ID, QID, QUESTION,
           TRIM(AI_COMPLETE('{LLM}',
             'Rewrite the user question below as a search query for a commercial contract. Keep the original wording, '
             || 'then add 4 to 6 alternative phrasings and synonyms that contract drafters typically use for the same concept '
             || '(for example: rate increase, price adjustment, fee escalation, indexation). '
             || 'Return ONE line of plain text and nothing else.\n\nQUESTION: ' || QUESTION)::STRING) AS EXPANDED
    FROM EVAL_QUESTIONS
""").collect()
EXPANSIONS = {(r[0], int(r[1])): r[2] for r in
              session.table("QUERY_EXPANSIONS").select("CONTRACT_ID", "QID", "EXPANDED").collect()}
session.table("QUERY_EXPANSIONS").filter("QID >= 100").select("QUESTION", "EXPANDED").limit(5).to_pandas()
''')
py(r'''
def retrieve_expanded(cid, q, qid=None):
    ctx = retrieve_orchestrated(cid, q, qid=qid)                     # original-query results first
    expanded = EXPANSIONS.get((cid, qid))
    if expanded and expanded != q:
        extra = search("CS_ENRICHED", expanded, ["SEARCH_TEXT"], limit=10, filt=in_contract(cid, CHUNK_TYPE="clause"))
        ctx += [h["SEARCH_TEXT"] for h in extra]
    return list(dict.fromkeys(ctx))

evaluate("E", retrieve_expanded)
''')

# ============================================================================ compare
md(r"""
## 7. Compare across the portfolio
All results live in `EVAL_RESULTS`, joined to `EVAL_QUESTIONS` and `CONTRACTS_META`.
With ~520 questions per config, differences of a few percentage points are meaningful. Single-contract runs aren't.
""")
py(r'''
import matplotlib.pyplot as plt

LABELS = {"A": "A. Baseline", "B": "B. Strategy 1 - section chunks + headers",
          "C": "C. Strategy 2 - + digest rows", "D": "D. Strategy 3 - + orchestrated retrieval",
          "E": "E. Strategy 4 - + query expansion"}

res = session.sql("""
    SELECT r.CONFIG, r.CORRECT, q.QTYPE, q.PHRASING, q.NEEDS_AMENDMENT, m.COMPLEXITY, m.STYLE, m.TYPE AS CONTRACT_TYPE,
           m.PAGE_BUCKET, m.HAS_EXHIBIT_D
    FROM EVAL_RESULTS r
    JOIN EVAL_QUESTIONS q USING (CONTRACT_ID, QID)
    JOIN CONTRACTS_META m USING (CONTRACT_ID)
""").to_pandas()   # bounded: ~520 rows x 4 configs
res["STRATEGY"] = res.CONFIG.map(LABELS)
order = [LABELS[k] for k in sorted(res.CONFIG.unique())]

def acc(by):
    t = res.pivot_table(index=by, columns="STRATEGY", values="CORRECT", aggfunc="mean")[order]
    return (100 * t).round(0)

overall = (100 * res.groupby("STRATEGY")["CORRECT"].mean()).reindex(order).round(0)
fig, ax = plt.subplots(figsize=(10, 3.5))
ax.barh(overall.index[::-1], overall.values[::-1])
for y, v in enumerate(overall.values[::-1]):
    ax.text(v + 1, y, f"{v:.0f}%", va="center")
ax.set_xlim(0, 105); ax.set_xlabel("% correct across all contracts and questions")
ax.set_title(f"Portfolio accuracy ({res.groupby('CONFIG').size().iloc[0]} questions per config)")
plt.tight_layout(); plt.show()
''')
py(r'''
# Where does each strategy help? Accuracy (%) by dimension.
def plot_dim(by, title):
    t = acc(by)
    t.plot(kind="bar", figsize=(11, 4), ylim=(0, 105), title=title, rot=0)
    plt.ylabel("% correct"); plt.legend(loc="lower right", fontsize=8); plt.tight_layout(); plt.show()
    return t

plot_dim("COMPLEXITY", "Accuracy by contract complexity (low = no amendments; high = 2-3 amendments, stacked overrides)")
''')
py(r'''
plot_dim("NEEDS_AMENDMENT", "Questions whose correct answer depends on an amendment (True) vs. not (False)")
''')
py(r'''
plot_dim("PAGE_BUCKET", "Accuracy by contract length (pages)")
''')
py(r'''
plot_dim("STYLE", "Accuracy by drafting style")
''')
md(r"""
### Synonyms and paraphrases
The same questions, asked in the contract's own words (`original`) and in everyday words (`paraphrase`).
The **paraphrase gap** (original minus paraphrase) shows how much each strategy depends on the user matching the
contract's vocabulary. Smaller is better.
""")
py(r'''
if (res.PHRASING == "paraphrase").any():
    phr = plot_dim("PHRASING", "Contract wording (original) vs. everyday wording (paraphrase)")
    gap = (phr.loc["original"] - phr.loc["paraphrase"]).rename("paraphrase gap (pts)").to_frame()
    display(gap)
    # Which question types suffer most from paraphrasing, baseline vs. best config?
    pq = res.pivot_table(index="QTYPE", columns=["STRATEGY", "PHRASING"], values="CORRECT", aggfunc="mean")
    display((100 * pq[[order[0], order[-1]]]).round(0))
else:
    print("No paraphrase questions in this run (INCLUDE_PARAPHRASES = False).")
''')
py(r'''
# Question type: the most diagnostic view. Rows sorted by baseline accuracy (hardest first).
qt = acc("QTYPE").sort_values(order[0])
qt
''')
py(r'''
# Per-contract: where is the remaining error after the best config?
best = order[-1]
per_contract = (100 * session.sql("""
    SELECT r.CONTRACT_ID, r.CONFIG, AVG(IFF(r.CORRECT, 1, 0)) AS ACC
    FROM EVAL_RESULTS r GROUP BY 1, 2
""").to_pandas().pivot(index="CONTRACT_ID", columns="CONFIG", values="ACC")).round(0)
per_contract.columns = [LABELS[c] for c in per_contract.columns]
meta_pd = session.table("CONTRACTS_META").select("CONTRACT_ID", "TYPE", "STYLE", "COMPLEXITY", "PDF_PAGES", "AMENDMENTS").to_pandas()
per_contract = meta_pd.set_index("CONTRACT_ID").join(per_contract).sort_values(best)
per_contract.head(15)
''')
sql("remaining_misses", r"""
-- Inspect the misses that remain in the best config: question, expected answer, model answer, judge reasoning.
SELECT r.CONTRACT_ID, q.QTYPE, q.QUESTION, q.EXPECTED, LEFT(r.ANSWER, 400) AS ANSWER, r.VERDICT
FROM {{DB}}.{{SCHEMA}}.EVAL_RESULTS r JOIN {{DB}}.{{SCHEMA}}.EVAL_QUESTIONS q USING (CONTRACT_ID, QID)
WHERE r.CONFIG = 'D' AND NOT r.CORRECT
ORDER BY q.QTYPE, r.CONTRACT_ID
""")

# ============================================================================ takeaways
md(r"""
## 8. Reading the results
Look for these patterns. They are the evidence to bring to the customer:

| What to look for | What it means |
|---|---|
| Baseline accuracy falls as **complexity** rises and when **NEEDS_AMENDMENT = True** | The core problem: answers whose pieces are spread across distant text are missed by top-k chunk retrieval |
| Strategy 1 lifts definition/exhibit questions but not amendment questions | Accurate headers help self-contained sections; short amendment clauses still lack topic words |
| Strategy 2 closes most of the **amendment** and **stacked-override** gap | The digest resolves "current value after all amendments" at ingest, so one retrieved piece holds the answer |
| Little change across **drafting styles** after Strategy 2 | Digests normalize vocabulary; the answer is no longer tied to how the clause was worded |
| Baseline shows a large **paraphrase gap** that shrinks with Strategy 2 and again with Strategy 4 | Semantic search handles many synonyms by itself. Digests restate terms in standard vocabulary, and query expansion bridges the remaining wording gaps |
| Remaining misses cluster in a few **question types** | Targeted fixes: a dedicated digest, `AI_EXTRACT` into structured fields, or a Cortex Agent tool |

| Strategy | Approach | Cost |
|---|---|---|
| 1 | Normalize headings, split by section (≤512 tokens), prefix each chunk with contract metadata, part, and section | Ingest-time SQL only |
| 2 | One LLM pass per contract writes topic-split digests (current terms, definitions, rights & remedies) indexed with the clauses | 3 long-context LLM calls per contract version |
| 3 | Several contract-scoped, filtered searches per question (clauses, digests, amendments) plus neighbouring chunks | A few extra search calls per question |
| 4 | LLM rewrites each question with contract-style synonyms; results from the expanded query are added to (not swapped for) the original query's results | 1 small LLM call + 1 extra search per question |
| Next | Cortex Agent orchestrating search + structured `AI_EXTRACT` fields for portfolio-wide questions | Agent orchestration cost |

**Caveat:** this is synthetic data. Real contracts have messier layouts, scanned pages, and question types not in
this set. Use this notebook to explain the *mechanism*, then re-run the harness on customer-approved documents
with their real questions.
""")

# ============================================================================ cleanup
md(r"""
## 9. Cleanup (optional)
Search services incur serving cost while they exist. Uncomment to remove everything this notebook created.
""")
py(r'''
# for svc in ["CS_BASELINE", "CS_CONTEXTUAL", "CS_ENRICHED"]:
#     session.sql(f"DROP CORTEX SEARCH SERVICE IF EXISTS {DB}.{SCHEMA}.{svc}").collect()
# session.sql(f"DROP SCHEMA IF EXISTS {DB}.{SCHEMA}").collect()
''')

nb = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"codemirror_mode": {"name": "ipython", "version": 3}, "file_extension": ".py",
                          "mimetype": "text/x-python", "name": "python", "nbconvert_exporter": "python",
                          "pygments_lexer": "ipython3", "version": "3.11.0"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}
out = Path(__file__).resolve().parent.parent / "cortex_search_multi_contract.ipynb"   # repo root
out.write_text(json.dumps(nb, indent=1))
print(f"wrote {out} ({len(cells)} cells)")
