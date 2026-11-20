"""Builds cortex_search_custom_contracts.ipynb: run the Config A-E strategies on YOUR OWN contracts and questions.

Unlike the 50-contract benchmark (exact computed answers), this notebook evaluates open-ended questions against
the clauses a correct answer must draw on (e.g. "6.1(a)", "Schedule A", "6.3", "7.2"), and reports per config:
  - section recall: were those clauses present in the retrieved context?
  - citation: did the answer cite them?
  - judge verdict against an expected-answer rubric.
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
# Cortex Search on Your Own Contracts — Clause Recall Evaluation

Companion to `cortex_search_multi_contract.ipynb`. It runs the same five configurations on **your contracts** and
**your questions**, and shows which strategy surfaces the clauses a correct answer has to draw on.

| Config | Strategy | Approach |
|---|---|---|
| A | Baseline | Fixed 1,800-char chunks, no metadata, top-5 retrieval (typical first implementation) |
| B | Strategy 1 | Numbered-heading normalization + section-aware chunks + contextual header (contract, parties, article, section) |
| C | Strategy 2 | B + LLM-generated document digests, split by topic, indexed with the clauses |
| D | Strategy 3 | C + orchestrated retrieval (top 15 clauses, digests, amendments, neighbouring chunks) |
| E | Strategy 4 | D + LLM query expansion; expanded-query results are **added to** the original results |

### How a question is scored
Open-ended questions ("What arguments can … justify a rate increase beyond 8%?") have no single right number.
Instead each question lists the **sections** a complete answer must use, plus a short expected-answer rubric:

| Metric | Meaning |
|---|---|
| **Section recall** | Share of expected sections whose text appears in the retrieved context. A section that isn't retrieved can't be used. |
| **Cited** | Expected sections the answer explicitly cites |
| **Judge** | LLM verdict: does the answer cover the rubric's key points? |

Each question is also run in `N_VARIANTS` LLM-written rewordings, to show whether results hold when users phrase it differently.

### Setup
1. Put the contract PDFs in a workspace folder (default `custom_contracts/`, next to this notebook). Any filenames.
2. Edit `QUESTIONS` in the parameters cell: the question, the contract it applies to (or `None` for all), the expected
   sections, and a short expected answer.
3. Run all cells. A single 10–20 page contract takes roughly 10–15 minutes, mostly building the 3 search services.

No manifest or answer key is needed. Contract title, parties, and effective date are extracted with `AI_EXTRACT`.
""")

# ============================================================================ params
md(r"""
## 0. Parameters
""")
py(r'''
# ---- Edit me ----------------------------------------------------------------
DB          = "CORTEX_SEARCH_DEMO"
SCHEMA      = "CUSTOM_CONTRACTS"         # kept separate from the benchmark schema
WAREHOUSE   = "COMPUTE_WH"
STAGE       = "CUSTOM_CONTRACT_STAGE"
LLM         = "claude-sonnet-4-5"        # answer, judge, digest, variant and expansion model
CUSTOM_DIR  = "custom_contracts"         # folder with your PDFs (relative to this notebook)
MAX_WORKERS = 8
N_VARIANTS  = 3                          # LLM rewordings per question: casual / formal / search-box (0 = original only)

# One entry per question. "contract": PDF file name without .pdf, or None to search all loaded contracts.
# "sections": what a complete answer must draw on. Use "6.1(a)", "6.3", "7.2" (matched to the numbered section)
#             or a part label such as "Schedule A" / "Exhibit B" / "Amendment No. 1".
QUESTIONS = [
    {
        "question": "What arguments can an organization use to justify a rate increase beyond 8% FERC?",
        "contract": "Enterprise_Ridgeway_Harborview_TSA_SAMPLE",
        "sections": ["6.1(a)", "Schedule A", "6.3", "7.2"],
        "expected": (
            "The 8% Escalation Cap (6.1(a), Schedule A) limits only the annual FERC-index escalation: FERC Index "
            "above 8% is banked and applied in later Contract Years, and the cap does not limit adjustments under "
            "6.1(b), 6.3(c)/(d) or Article 7. Under 6.3, a successor index with a higher multiplier applies; a FERC/TRRC "
            "cost-of-service order or finding that the rate is below just and reasonable lets the Carrier conform the "
            "rate without regard to the cap; an accepted 18 CFR 342.4 filing (cost-of-service or substantial divergence) "
            "is conformed and not counted toward the cap. Under 7.2, a change in law or emission requirement costing "
            "over $22.5M over three years lets the Carrier increase rates or add a surcharge to recover its costs."
        ),
    },
]

# Document digests (Strategy 2). Generic contract topics; add or edit for your document type.
DIGESTS = {
    "Current terms after all amendments":
        "List the CURRENT effective commercial and legal terms of this agreement after applying every amendment in date order and any order-of-precedence clause. Use a markdown heading per topic. For each term give the current value, the original value if changed, and the clause it comes from.",
    "Defined terms and cross-references":
        "List every defined term in this agreement under its own markdown heading. Give its meaning, fully resolve chains of cross-references to other sections, schedules, exhibits and amendments, and state what the term includes and excludes.",
    "Rights, remedies and termination":
        "Summarise, under markdown headings, every termination right, renewal mechanism, termination fee, cure period, exclusive or non-exclusive remedy, liability cap and carve-out in this agreement, with clause references and exceptions.",
    "Pricing, rates and adjustment mechanisms":
        "Under markdown headings, describe every way prices, rates, fees or charges under this agreement are set, escalated, capped, adjusted, surcharged or recovered, including caps, carry-forwards, exceptions to caps, regulatory or index-based adjustments, change-in-law and cost-recovery provisions. Cite each clause.",
}
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
## 1. Load and parse the contracts, and extract metadata
""")
py(r'''
import glob, json, os, re
import pandas as pd

pdfs = sorted(glob.glob(os.path.join(CUSTOM_DIR, "*.pdf")))

session.sql(f"""
    CREATE STAGE IF NOT EXISTS {STAGE}
      ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE') DIRECTORY = (ENABLE = TRUE)
""").collect()
# Two ways to supply contracts:
#   1. PDFs in the workspace folder CUSTOM_DIR -> uploaded to the stage here, or
#   2. PDFs already on the stage (e.g. uploaded in Snowsight or with PUT) -> used as-is.
for p in pdfs:
    session.file.put(p, f"@{STAGE}", auto_compress=False, overwrite=True)
session.sql(f"ALTER STAGE {STAGE} REFRESH").collect()

staged = [r[0] for r in session.sql(f"SELECT RELATIVE_PATH FROM DIRECTORY(@{STAGE}) WHERE RELATIVE_PATH ILIKE '%.pdf'").collect()]
assert staged, (f"No PDFs found in workspace folder {CUSTOM_DIR!r} or on stage {STAGE}. "
                "Upload contracts to either location.")
loaded = sorted(re.sub(r"(?i)[.]pdf$", "", os.path.basename(p)) for p in staged)
print(f"{len(pdfs)} uploaded from {CUSTOM_DIR!r}; {len(loaded)} contract(s) on stage {STAGE}:", loaded)

# Parse once (LAYOUT mode keeps headings and tables).
session.sql(f"""
    CREATE OR REPLACE TABLE PARSED_DOCS AS
    SELECT REGEXP_REPLACE(RELATIVE_PATH, '[.]pdf$', '', 1, 1, 'i') AS CONTRACT_ID,
           AI_PARSE_DOCUMENT(TO_FILE('@{STAGE}', RELATIVE_PATH), {{'mode': 'LAYOUT'}}):content::STRING AS CONTENT
    FROM DIRECTORY(@{STAGE})
    WHERE RELATIVE_PATH ILIKE '%.pdf'
""").collect()
session.sql(f"DELETE FROM PARSED_DOCS WHERE CONTRACT_ID NOT IN ({','.join('?' * len(loaded))})", params=loaded).collect()

# Contract metadata from the documents themselves (replaces a CLM system / manifest).
session.sql(f"""
    CREATE OR REPLACE TABLE CONTRACTS_META AS
    SELECT CONTRACT_ID,
           COALESCE(m:response:title::STRING, CONTRACT_ID)           AS TITLE,
           COALESCE(m:response:party_a::STRING, 'unknown party')     AS PROVIDER,
           COALESCE(m:response:party_b::STRING, 'unknown party')     AS CUSTOMER,
           COALESCE(m:response:effective_date::STRING, 'unknown')    AS EFFECTIVE_DATE
    FROM (
        SELECT REGEXP_REPLACE(RELATIVE_PATH, '[.]pdf$', '', 1, 1, 'i') AS CONTRACT_ID,
               AI_EXTRACT(file => TO_FILE('@{STAGE}', RELATIVE_PATH), responseFormat => {{
                   'title': 'What is the title of this agreement?',
                   'party_a': 'Who is the first named party (full legal name)?',
                   'party_b': 'Who is the second named party (full legal name)?',
                   'effective_date': 'What is the effective date of the agreement?'}}) AS m
        FROM DIRECTORY(@{STAGE})
        WHERE RELATIVE_PATH ILIKE '%.pdf'
    )
    WHERE CONTRACT_ID IN (SELECT CONTRACT_ID FROM PARSED_DOCS)
""").collect()
session.table("CONTRACTS_META").to_pandas()
''')
py(r'''
# Questions table: original wording plus N_VARIANTS LLM rewordings. CONTRACT_ID '*' = search all contracts.
default_cid = loaded[0] if len(loaded) == 1 else "*"
rows = []
for i, q in enumerate(QUESTIONS):
    cid = q.get("contract") or default_cid
    assert cid == "*" or cid in loaded, f"Question {i}: contract {cid!r} not among loaded PDFs {loaded}"
    rows.append({"CONTRACT_ID": cid, "QNUM": i, "QID": i * 100, "VARIANT": "original", "QUESTION": q["question"],
                 "EXPECTED": q["expected"], "SECTIONS": json.dumps(q["sections"])})
questions = pd.DataFrame(rows)
session.create_dataframe(questions).write.mode("overwrite").save_as_table("EVAL_QUESTIONS")

if N_VARIANTS:
    # One call per question returns N stylistically different rewordings (separate calls tend to return near-duplicates).
    session.sql(f"""
        INSERT INTO EVAL_QUESTIONS (CONTRACT_ID, QNUM, QID, VARIANT, QUESTION, EXPECTED, SECTIONS)
        WITH gen AS (
            SELECT q.*, AI_COMPLETE('{LLM}',
                     'Write {int(N_VARIANTS)} different rewordings of the question below, as different business users might ask it '
                     || '(for example: casual and conversational, formal and precise, terse like a search-box query). '
                     || 'Keep the exact meaning; vary the vocabulary as much as possible. Return exactly {int(N_VARIANTS)} lines, '
                     || 'one rewording per line, with no numbering or commentary.\n\nQUESTION: ' || q.QUESTION)::STRING AS T
            FROM EVAL_QUESTIONS q WHERE q.VARIANT = 'original'
        ), lines AS (
            SELECT gen.*, TRIM(REGEXP_REPLACE(f.VALUE::STRING, '^[0-9]+[.)] *|^[-*] *', '')) AS V,
                   ROW_NUMBER() OVER (PARTITION BY gen.QID ORDER BY f.INDEX) AS N
            FROM gen, LATERAL FLATTEN(SPLIT(TRIM(gen.T), '\n')) f
            WHERE TRIM(f.VALUE::STRING) <> ''              -- number only non-blank lines
        )
        SELECT CONTRACT_ID, QNUM, QID + N, 'variant ' || N, V, EXPECTED, SECTIONS
        FROM lines WHERE N <= {int(N_VARIANTS)}
    """).collect()
session.table("EVAL_QUESTIONS").select("QID", "VARIANT", "QUESTION").sort("QID").to_pandas()
''')

# ============================================================================ harness
md(r"""
## 2. Evaluation harness
Same pattern as the benchmark notebook. Retrieval runs in a thread pool into `EVAL_CONTEXTS`. Answering and judging
run as one batched SQL statement per config into `EVAL_RESULTS`. Section recall is computed at the end from the
stored contexts, so every config is measured the same way.
""")
py(r'''
from concurrent.futures import ThreadPoolExecutor

def fqn(name):
    return f"{DB}.{SCHEMA}.{name}"

def search(service, query, columns, limit=5, filt=None):
    payload = {"query": query, "columns": columns, "limit": limit}
    if filt:
        payload["filter"] = filt
    body = json.dumps(payload).replace("$$", "$ $")
    raw = session.sql(f"SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW('{fqn(service)}', $${body}$$)").collect()[0][0]
    return json.loads(raw)["results"]

def in_scope(cid, **eq):
    """Filter to one contract (or all, when cid == '*'), optionally ANDed with attribute filters."""
    conds = ([] if cid == "*" else [{"@eq": {"CONTRACT_ID": cid}}]) + [{"@eq": {k: v}} for k, v in eq.items()]
    return None if not conds else (conds[0] if len(conds) == 1 else {"@and": conds})

session.sql("CREATE OR REPLACE TABLE EVAL_CONTEXTS (CONFIG STRING, CONTRACT_ID STRING, QID INT, N_CTX INT, CONTEXT STRING)").collect()
session.sql("CREATE OR REPLACE TABLE EVAL_RESULTS (CONFIG STRING, CONTRACT_ID STRING, QID INT, ANSWER STRING, VERDICT STRING, CORRECT BOOLEAN)").collect()

def run_retrieval(config, retrieve_fn):
    qs = session.table("EVAL_QUESTIONS").select("CONTRACT_ID", "QID", "QUESTION").to_pandas()
    def one(r):
        ctx = retrieve_fn(r.CONTRACT_ID, r.QUESTION, qid=int(r.QID))
        return {"CONFIG": config, "CONTRACT_ID": r.CONTRACT_ID, "QID": int(r.QID), "N_CTX": len(ctx),
                "CONTEXT": "\n\n---\n\n".join(ctx)}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        out = list(pool.map(one, qs.itertuples(index=False)))
    session.sql("DELETE FROM EVAL_CONTEXTS WHERE CONFIG = ?", params=[config]).collect()
    session.create_dataframe(pd.DataFrame(out)).write.mode("append").save_as_table("EVAL_CONTEXTS")

def score(config):
    session.sql("DELETE FROM EVAL_RESULTS WHERE CONFIG = ?", params=[config]).collect()
    session.sql(f"""
        INSERT INTO EVAL_RESULTS
        WITH answered AS (
            SELECT c.CONFIG, c.CONTRACT_ID, c.QID, q.QUESTION, q.EXPECTED, q.SECTIONS,
                   AI_COMPLETE('{LLM}',
                     'You are a contract analyst. Answer ONLY from the excerpts below. Identify every relevant '
                     || 'provision, including exceptions and provisions in other sections that qualify or override it. '
                     || 'Cite the section or schedule for each point.\n\nEXCERPTS:\n' || c.CONTEXT
                     || '\n\nQUESTION: ' || q.QUESTION
                   )::STRING AS ANSWER
            FROM EVAL_CONTEXTS c JOIN EVAL_QUESTIONS q USING (CONTRACT_ID, QID)
            WHERE c.CONFIG = ?
        ), judged AS (
            SELECT *, AI_COMPLETE('{LLM}',
                     'Grade the ANSWER against the EXPECTED answer rubric. It is CORRECT only if it covers the main '
                     || 'points of the rubric drawing on these sections: ' || SECTIONS || '. Extra correct detail is fine; '
                     || 'missing a whole section''s argument makes it INCORRECT. Reply with exactly one word on the first '
                     || 'line, CORRECT or INCORRECT, then one sentence naming any missing points.'
                     || '\n\nQUESTION: ' || QUESTION || '\nEXPECTED: ' || EXPECTED || '\nANSWER: ' || ANSWER
                   )::STRING AS VERDICT
            FROM answered
        )
        SELECT CONFIG, CONTRACT_ID, QID, ANSWER, VERDICT,
               STARTSWITH(UPPER(LTRIM(VERDICT, ' "*#\n')), 'CORRECT') AS CORRECT
        FROM judged
    """, params=[config]).collect()

def evaluate(config, retrieve_fn):
    run_retrieval(config, retrieve_fn)
    score(config)
    n, ok = session.sql("SELECT COUNT(*), COUNT_IF(CORRECT) FROM EVAL_RESULTS WHERE CONFIG = ?", params=[config]).collect()[0]
    print(f"{config}: judge {ok}/{n} correct")

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

# ============================================================================ A
md(r"""
## 3. Config A — Baseline (typical first implementation)
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
    return [h["SEARCH_TEXT"] for h in search("CS_BASELINE", q, ["SEARCH_TEXT"], limit=5, filt=in_scope(cid))]

evaluate("A", retrieve_baseline)
''')

# ============================================================================ B
md(r"""
## 4. Config B — Strategy 1: numbered-heading normalization + contextual chunks
Commercial contracts number headings in different ways, and `AI_PARSE_DOCUMENT` marks them inconsistently (`#`, `##`,
or plain text). For example, `6.` may come back as a heading while `7.` doesn't, and `6.1` / `7.2` come back as plain
paragraphs. Before splitting, the text is normalized:

| Source pattern | Normalized to |
|---|---|
| `SCHEDULE A`, `EXHIBIT B`, `APPENDIX`, `ANNEX`, `AMENDMENT NO. 1` | `# <part>` |
| `6. Transportation Rates` (number + title on its own line) | `# Article 6. Transportation Rates` |
| `Section 9. Data Ownership` | `## Section 9. Data Ownership` |
| `6.1 In addition to …` (subsection at the start of a line) | `## Section 6.1` + the original line |

Every chunk then gets a header with contract title, parties, effective date, article, and section.
""")
py(r'''
session.sql(r"""
    CREATE OR REPLACE TABLE CHUNKS_CONTEXTUAL AS
    WITH norm AS (
        SELECT CONTRACT_ID,
          REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(CONTENT,
            '^[#* ]*((SCHEDULE|Schedule|EXHIBIT|Exhibit|APPENDIX|Appendix|ANNEX|Annex|AMENDMENT NO[.]|Amendment No[.])( [A-Z0-9]+)?[^\n]{0,60})$', '# \\1', 1, 0, 'm'),
            '^[#* ]*(Section [0-9A-Za-z-]+[.][^\n*]*)[*]*$', '## \\1', 1, 0, 'm'),
            '^[#* ]*([0-9]{1,2})[.] +([A-Z][^\n]{0,80})$', '# Article \\1. \\2', 1, 0, 'm'),
            '^[#* ]*([0-9]{1,2}[.][0-9]{1,2})[ ]+', '## Section \\1\n\\1 ', 1, 0, 'm') AS CONTENT
        FROM PARSED_DOCS
    ), s AS (
        SELECT n.CONTRACT_ID, c.INDEX AS CHUNK_ID, c.VALUE:chunk::STRING AS BODY,
               COALESCE(c.VALUE:headers:h1::STRING, 'Preamble') AS PART,
               COALESCE(c.VALUE:headers:h2::STRING, '')         AS SECTION
        FROM norm n,
             LATERAL FLATTEN(SNOWFLAKE.CORTEX.SPLIT_TEXT_MARKDOWN_HEADER(
                 n.CONTENT, OBJECT_CONSTRUCT('#', 'h1', '##', 'h2'), 1200, 200)) c
    )
    SELECT s.CONTRACT_ID, s.CHUNK_ID, s.PART, s.SECTION, s.BODY, 'clause' AS CHUNK_TYPE,
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
-- Detected structure per contract: check the expected sections show up as their own PART / SECTION labels.
SELECT CONTRACT_ID, PART, SECTION, COUNT(*) AS CHUNKS
FROM {{DB}}.{{SCHEMA}}.CHUNKS_CONTEXTUAL
GROUP BY 1, 2, 3 ORDER BY 1, MIN(CHUNK_ID)
""")
py(r'''
ATTRS = ["CONTRACT_ID", "PART", "SECTION", "CHUNK_TYPE", "IS_AMENDMENT"]
build_service("CS_CONTEXTUAL", "CHUNKS_CONTEXTUAL", ATTRS)

def retrieve_contextual(cid, q, qid=None):
    return [h["SEARCH_TEXT"] for h in search("CS_CONTEXTUAL", q, ["SEARCH_TEXT"], limit=5, filt=in_scope(cid))]

evaluate("B", retrieve_contextual)
''')

# ============================================================================ C
md(r"""
## 5. Config C — Strategy 2: document digests
One LLM pass per contract per digest in `DIGESTS`, over the **full** text. Each digest is split by its own headings
into pieces of ≤1,500 characters and indexed next to the clauses. The pricing/adjustment digest is a generic topic for
commercial agreements. It isn't tuned to any one question, but it's the digest most relevant to rate questions.
""")
py(r'''
digest_df = pd.DataFrame([{"DIGEST_ID": i + 1, "DIGEST_TITLE": t, "INSTRUCTION": ins} for i, (t, ins) in enumerate(DIGESTS.items())])
session.create_dataframe(digest_df).write.mode("overwrite").save_as_table("DIGEST_PROMPTS")

session.sql(f"""
    CREATE OR REPLACE TABLE DIGEST_RAW AS
    SELECT p.CONTRACT_ID, d.DIGEST_ID, d.DIGEST_TITLE,
           AI_COMPLETE('{LLM}', d.INSTRUCTION || '\n\nAGREEMENT:\n' || p.CONTENT, {{'max_tokens': 8192}})::STRING AS DIGEST
    FROM PARSED_DOCS p CROSS JOIN DIGEST_PROMPTS d
""").collect()

session.sql(r"""
    CREATE OR REPLACE TABLE CHUNKS_ENRICHED AS
    SELECT CONTRACT_ID, CHUNK_ID, PART, SECTION, CHUNK_TYPE, IS_AMENDMENT, SEARCH_TEXT FROM CHUNKS_CONTEXTUAL
    UNION ALL
    SELECT d.CONTRACT_ID, 100000 + d.DIGEST_ID * 1000 + c.INDEX, 'Document digest', d.DIGEST_TITLE, 'digest', 'N',
           'Contract: ' || m.TITLE || ' between ' || m.PROVIDER || ' and ' || m.CUSTOMER
             || '\nDigest: ' || d.DIGEST_TITLE
             || IFF(c.VALUE:headers:h2 IS NOT NULL OR c.VALUE:headers:h1 IS NOT NULL,
                    '\nTopic: ' || COALESCE(c.VALUE:headers:h2::STRING, c.VALUE:headers:h1::STRING), '')
             || '\n\n' || c.VALUE:chunk::STRING
    FROM DIGEST_RAW d JOIN CONTRACTS_META m USING (CONTRACT_ID),
         LATERAL FLATTEN(SNOWFLAKE.CORTEX.SPLIT_TEXT_MARKDOWN_HEADER(
             d.DIGEST, OBJECT_CONSTRUCT('#', 'h1', '##', 'h2'), 1500, 150)) c
""").collect()
session.sql("ALTER TABLE CHUNKS_ENRICHED SET CHANGE_TRACKING = TRUE").collect()
build_service("CS_ENRICHED", "CHUNKS_ENRICHED", ATTRS)

def retrieve_enriched(cid, q, qid=None):
    return [h["SEARCH_TEXT"] for h in search("CS_ENRICHED", q, ["SEARCH_TEXT"], limit=5, filt=in_scope(cid))]

evaluate("C", retrieve_enriched)
''')

# ============================================================================ D
md(r"""
## 6. Config D — Strategy 3: orchestrated retrieval
""")
py(r'''
def neighbors(cid, chunk_ids):
    ids = sorted({int(c) + d for c in chunk_ids for d in (-1, 1)} - {int(c) for c in chunk_ids})
    if not ids:
        return []
    where_cid = "" if cid == "*" else "CONTRACT_ID = ? AND "
    rows = session.sql(
        f"SELECT SEARCH_TEXT FROM CHUNKS_ENRICHED WHERE {where_cid}CHUNK_TYPE = 'clause' "
        f"AND CHUNK_ID IN ({','.join(map(str, ids))})", params=[] if cid == "*" else [cid]).collect()
    return [r[0] for r in rows]

def retrieve_orchestrated(cid, q, qid=None):
    cols = ["SEARCH_TEXT", "CHUNK_ID"]
    clauses = search("CS_ENRICHED", q, cols, limit=15, filt=in_scope(cid, CHUNK_TYPE="clause"))
    digests = search("CS_ENRICHED", q, cols, limit=3,  filt=in_scope(cid, CHUNK_TYPE="digest"))
    amends  = search("CS_ENRICHED", q, cols, limit=3,  filt=in_scope(cid, IS_AMENDMENT="Y"))
    ctx = [h["SEARCH_TEXT"] for h in digests + amends + clauses]
    ctx += neighbors(cid, [h["CHUNK_ID"] for h in clauses[:3]])
    return list(dict.fromkeys(ctx))

evaluate("D", retrieve_orchestrated)
''')

# ============================================================================ E
md(r"""
## 7. Config E — Strategy 4: query expansion
An LLM rewrites each question with contract-style synonyms. The prompt is domain-neutral (no example vocabulary), so
it doesn't steer every expansion toward one topic. The expanded query's top 10 clauses are **added to** Config D's results.
""")
py(r'''
session.sql(f"""
    CREATE OR REPLACE TABLE QUERY_EXPANSIONS AS
    SELECT CONTRACT_ID, QID, QUESTION,
           TRIM(AI_COMPLETE('{LLM}',
             'Rewrite the user question below as a search query for a commercial contract. Keep the original wording, '
             || 'then add 4 to 6 alternative phrasings and synonyms that contract drafters typically use for the same '
             || 'concepts in the question. Return ONE line of plain text and nothing else.\n\nQUESTION: ' || QUESTION)::STRING) AS EXPANDED
    FROM EVAL_QUESTIONS
""").collect()
EXPANSIONS = {(r[0], int(r[1])): r[2] for r in session.table("QUERY_EXPANSIONS").select("CONTRACT_ID", "QID", "EXPANDED").collect()}

def retrieve_expanded(cid, q, qid=None):
    ctx = retrieve_orchestrated(cid, q, qid=qid)
    expanded = EXPANSIONS.get((cid, qid))
    if expanded and expanded != q:
        ctx += [h["SEARCH_TEXT"] for h in search("CS_ENRICHED", expanded, ["SEARCH_TEXT"], limit=10,
                                                 filt=in_scope(cid, CHUNK_TYPE="clause"))]
    return list(dict.fromkeys(ctx))

evaluate("E", retrieve_expanded)
session.table("QUERY_EXPANSIONS").select("QUESTION", "EXPANDED").to_pandas()
''')

# ============================================================================ results
md(r"""
## 8. Results: which strategy surfaces the required clauses?
**Section recall** checks whether each expected section's **verbatim text** appears in the context passed to the
LLM. For each expected section, the clause text is taken from the section-aware chunks (Config B's labels), and the
check looks for short snippets of it in each config's retrieved context, ignoring whitespace. Digest rows paraphrase
the contract, so they don't count toward recall. Their effect shows up in **Cited** and **Judge**.
""")
py(r'''
import matplotlib.pyplot as plt

LABELS = {"A": "A. Baseline", "B": "B. Strategy 1 - section chunks + headers", "C": "C. Strategy 2 - + digests",
          "D": "D. Strategy 3 - + orchestrated retrieval", "E": "E. Strategy 4 - + query expansion"}
norm = lambda s: re.sub(r"\s+", " ", s or "").strip()

# Snippets of each section's clause text, from the section-aware chunks.
chunks = session.table("CHUNKS_CONTEXTUAL").select("CONTRACT_ID", "PART", "SECTION", "BODY").to_pandas()
def section_rows(cid, label):
    m = re.match(r"^\s*(\d+\.\d+)", label)
    sel = chunks if cid == "*" else chunks[chunks.CONTRACT_ID == cid]
    if m:
        return sel[sel.SECTION.str.fullmatch(f"Section {re.escape(m.group(1))}")]
    return sel[sel.PART.str.lower().str.startswith(label.lower())]

def snippets(cid, label):
    out = []
    for body in section_rows(cid, label).BODY:
        b = norm(body)
        for start in range(20, max(21, len(b) - 90), 250):       # several snippets per chunk
            if len(b[start:start + 70]) >= 40:
                out.append(b[start:start + 70])
    return out

qs = session.table("EVAL_QUESTIONS").to_pandas()
ctx = session.table("EVAL_CONTEXTS").to_pandas()
resd = session.table("EVAL_RESULTS").to_pandas()
df = ctx.merge(qs, on=["CONTRACT_ID", "QID"]).merge(resd, on=["CONFIG", "CONTRACT_ID", "QID"])

def cites(answer, label):
    m = re.match(r"^\s*(\d+\.\d+)", label)
    pat = re.escape(m.group(1)) + r"(?!\d)" if m else re.escape(label)
    return re.search(pat, answer or "", re.I) is not None

rows = []
missing_snippets = set()
for r in df.itertuples(index=False):
    c = norm(r.CONTEXT)
    for label in json.loads(r.SECTIONS):
        snips = snippets(r.CONTRACT_ID, label)
        if not snips:
            missing_snippets.add(label)
        rows.append({"CONFIG": r.CONFIG, "QNUM": r.QNUM, "VARIANT": r.VARIANT, "SECTION": label,
                     "RETRIEVED": any(s in c for s in snips), "CITED": cites(r.ANSWER, label),
                     "JUDGE": bool(r.CORRECT)})
hits = pd.DataFrame(rows)
if missing_snippets:
    print("WARNING - no section text found for:", missing_snippets, "- check the section labels / structure_check")

summary = (hits.groupby("CONFIG")
               .agg(section_recall=("RETRIEVED", "mean"), cited=("CITED", "mean"))
               .join(df.groupby("CONFIG")["CORRECT"].mean().rename("judge_correct")))
summary = (100 * summary).round(0).rename(index=LABELS)
summary
''')
py(r'''
# Which expected sections were retrieved, per config (share of question variants in which each was found).
matrix = (100 * hits.pivot_table(index="SECTION", columns="CONFIG", values="RETRIEVED", aggfunc="mean")).round(0)
matrix.columns = [LABELS[c] for c in matrix.columns]
matrix
''')
py(r'''
ax = summary[["section_recall", "cited", "judge_correct"]].plot(kind="barh", figsize=(10, 4.5), xlim=(0, 105))
ax.invert_yaxis(); ax.set_xlabel("%"); ax.set_title("Required clauses retrieved, cited, and judged correct, by strategy")
plt.legend(loc="lower right"); plt.tight_layout(); plt.show()
''')
sql("answers", r"""
-- Read the answers side by side (original wording). The judge's sentence names any missing points.
SELECT r.CONFIG, q.VARIANT, q.QUESTION, r.ANSWER, r.VERDICT
FROM {{DB}}.{{SCHEMA}}.EVAL_RESULTS r JOIN {{DB}}.{{SCHEMA}}.EVAL_QUESTIONS q USING (CONTRACT_ID, QID)
ORDER BY q.QNUM, q.QID, r.CONFIG
""")

# ============================================================================ cleanup
md(r"""
## 9. Cleanup (optional)
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
out = Path(__file__).resolve().parent.parent / "cortex_search_custom_contracts.ipynb"
out.write_text(json.dumps(nb, indent=1))
print(f"wrote {out} ({len(cells)} cells)")
