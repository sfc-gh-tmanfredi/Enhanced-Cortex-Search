"""Generate 50 synthetic contracts (10-50 pages) for testing Cortex Search long-document retrieval.

Variation per contract:
  - type      : MSA, SaaS subscription, supply, software licence, consulting, data processing, commercial lease
  - style     : formal legalese, plain English, British English
  - complexity: low (no amendments), medium (1 amendment), high (2-3 amendments, stacked overrides,
                conflicting exhibit + order of precedence, definition chains)
  - length    : ~10-50 pages via boilerplate volume (diluting the planted facts)

Outputs (under ../contracts):
  pdf/contract_01.pdf ... pdf/contract_50.pdf, markdown/contract_01.md ... markdown/contract_50.md
  contracts_manifest.csv   one row per contract (type, style, complexity, pages, words)
  contracts_answer_key.json  questions + expected answers per contract, computed from the generated terms

Run:  uv run --with reportlab --with pypdf python scripts/generate_contracts.py
"""
import csv
import json
import random
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "contracts"   # repo layout
PDF_OUT, MD_OUT = OUT / "pdf", OUT / "markdown"
N_CONTRACTS = 50
WORDS_PER_PAGE = 500
rng = random.Random(2026)

# ----------------------------------------------------------------------------- vocab
PROVIDERS = ["Northwind Cloud Services, Inc.", "Fabrikam Systems Ltd", "Litware Analytics LLC", "Adventure Works Logistics Corp.",
             "Tailspin Data Partners plc", "Wingtip Software GmbH", "Proseware Technologies, Inc.", "Lucerne Supply Co.",
             "Coho Manufacturing Group", "Woodgrove Advisory LLP", "Blue Yonder Robotics, Inc.", "Alpine Ski Holdings Ltd"]
CUSTOMERS = ["Contoso Health Systems, LLC", "Relecloud Retail Inc.", "Margie's Travel Ltd", "Trey Research Corporation",
             "Humongous Insurance Co.", "Southridge Video plc", "Graphic Design Institute", "VanArsdel Foods Ltd",
             "Bellows College", "Datum Financial Group, Inc.", "First Up Consultants LLC", "Munson's Pickles Inc."]
LAWS = ["the State of New York", "the State of Delaware", "the State of California", "England and Wales",
        "the State of Illinois", "the Province of Ontario", "Scotland", "the State of Texas"]

CONTRACT_TYPES = {
    "MSA":        {"title": "Master Services Agreement",        "p": "Provider",  "c": "Customer",  "fee": "monthly service fee"},
    "SaaS":       {"title": "SaaS Subscription Agreement",      "p": "Vendor",    "c": "Subscriber", "fee": "monthly subscription fee"},
    "Supply":     {"title": "Supply Agreement",                 "p": "Supplier",  "c": "Buyer",      "fee": "monthly minimum purchase commitment"},
    "Licence":    {"title": "Software Licence Agreement",       "p": "Licensor",  "c": "Licensee",   "fee": "monthly licence fee"},
    "Consulting": {"title": "Consulting Services Agreement",    "p": "Consultant", "c": "Client",    "fee": "monthly retainer"},
    "DPA":        {"title": "Data Processing Services Agreement", "p": "Processor", "c": "Controller", "fee": "monthly processing fee"},
    "Lease":      {"title": "Commercial Equipment Lease",       "p": "Lessor",    "c": "Lessee",     "fee": "monthly rent"},
}

NUM_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
             10: "ten", 12: "twelve", 14: "fourteen", 15: "fifteen", 18: "eighteen", 20: "twenty", 24: "twenty-four",
             25: "twenty-five", 30: "thirty", 36: "thirty-six", 40: "forty", 45: "forty-five", 48: "forty-eight",
             60: "sixty", 72: "seventy-two", 90: "ninety", 120: "one hundred twenty", 180: "one hundred eighty"}

def num(n, style):
    """Legalese writes 'sixty (60)'; plain/British mostly digits. Inconsistency is deliberate (harder keyword match)."""
    if style == "legalese" and n in NUM_WORDS:
        return f"{NUM_WORDS[n]} ({n})"
    if style == "british" and n in NUM_WORDS and rng.random() < 0.4:
        return f"{NUM_WORDS[n]} ({n})"
    return str(n)

def money(x, style):
    sym = "£" if style == "british" and rng.random() < 0.6 else "$"
    return f"{sym}{x:,.0f}", sym

def fmt_date(y, m, d, style):
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]
    return f"{d} {months[m-1]} {y}" if style == "british" else f"{months[m-1]} {d}, {y}"

def add_months(y, m, k):
    t = (y * 12 + (m - 1)) + k
    return t // 12, t % 12 + 1

# ----------------------------------------------------------------------------- style-specific phrasing
STYLE = {
    "legalese": {
        "shall": "shall", "may": "may", "party_intro": "(each a \"Party\" and collectively the \"Parties\")",
        "whereas": True, "notwith": "Notwithstanding anything to the contrary herein,",
        "filler": [
            "With respect to {t}, {P} shall act in a commercially reasonable manner consistent with prevailing industry practice and the requirements of any applicable Order Form, and shall maintain written procedures governing {t}, which shall be made available to {C} upon reasonable prior written request not more frequently than once per calendar quarter.",
            "Each Party shall designate a representative responsible for {t}. Such representatives shall confer as reasonably required to coordinate the activities contemplated hereunder, and any agreed modifications shall be memorialised in writing; provided, however, that no such writing shall amend this Agreement unless executed in accordance with the amendment provisions hereof.",
            "{P} shall ensure that all activities relating to {t} comply with Applicable Law and the {C} Policies. In the event of any inconsistency between a {C} Policy and an Order Form, the Parties shall negotiate in good faith to resolve such inconsistency within {d} Business Days.",
            "{C} may, in its sole discretion, request reasonable modifications to the practices of {P} concerning {t}, whereupon {P} shall evaluate each such request and respond within {d} Business Days with a written assessment of the impact thereof on schedule, cost and the Service Levels.",
            "Nothing in this Section shall be construed as limiting the obligations of either Party with respect to {t} under any other provision of this Agreement, and the rights and remedies set forth herein are cumulative and in addition to any other rights or remedies available at law or in equity.",
        ],
    },
    "plain": {
        "shall": "must", "may": "can", "party_intro": "(together, \"we\" or the \"parties\")",
        "whereas": False, "notwith": "Even if another part of this agreement says otherwise,",
        "filler": [
            "For {t}, {P} will follow good industry practice and the relevant order form. {P} will keep written procedures for {t} and share them with {C} when asked, up to once a quarter.",
            "Each party will name a contact for {t}. The contacts will meet when needed and write down any agreed changes. Those notes do not change this agreement unless both parties sign a formal amendment.",
            "Everything {P} does for {t} must follow the law and {C}'s policies. If a policy and an order form disagree, the parties will work it out within {d} business days.",
            "{C} can ask for reasonable changes to how {P} handles {t}. {P} will reply within {d} business days explaining any effect on timing, cost, or service levels.",
            "This section does not limit either party's other obligations about {t}. The rights in this section add to any other rights a party has.",
        ],
    },
    "british": {
        "shall": "shall", "may": "may", "party_intro": "(each a \"Party\" and together the \"Parties\")",
        "whereas": True, "notwith": "Notwithstanding any other provision of this Agreement,",
        "filler": [
            "In relation to {t}, {P} shall act in accordance with good industry practice and the relevant Order Form, and shall maintain written procedures in respect of {t}, which it shall make available to {C} upon reasonable request, not more than once in any calendar quarter.",
            "Each Party shall nominate a representative with responsibility for {t}. The representatives shall meet as reasonably required, and any agreed variations shall be recorded in writing, save that no such record shall vary this Agreement unless signed by both Parties.",
            "{P} shall ensure that all activities in connection with {t} comply with Applicable Law and the {C} Policies whilst this Agreement remains in force. Where a {C} Policy conflicts with an Order Form, the Parties shall endeavour to resolve the conflict within {d} Working Days.",
            "{C} may request reasonable changes to the organisation of {P}'s practices regarding {t}. {P} shall consider each such request and respond within {d} Working Days setting out any impact on programme, cost and the Service Levels.",
            "Nothing in this clause shall limit either Party's obligations in respect of {t} elsewhere in this Agreement, and the rights set out herein are without prejudice to any other rights or remedies of either Party.",
        ],
    },
}

FILLER_TOPICS = ["operational reporting", "personnel qualifications", "change control", "documentation standards",
                 "governance meetings", "subcontractor oversight", "records retention", "knowledge transfer",
                 "quality assurance", "escalation procedures", "site access", "training", "equipment usage",
                 "environmental compliance", "background screening", "business continuity", "capacity planning",
                 "invoicing administration", "audit cooperation", "transition assistance"]
FILLER_TITLES = ["Scope of Services", "Order Forms", "Personnel", "Customer Obligations", "Intellectual Property",
                 "Warranties", "Indemnification", "Insurance", "Dispute Resolution", "Audit", "Subcontracting",
                 "Business Continuity", "Compliance", "Transition Services", "Governance", "Reporting",
                 "Records", "Publicity", "Export Control", "Anti-Corruption", "Force Majeure", "Assignment",
                 "Non-Solicitation", "Third-Party Software", "Acceptance", "Change Control", "Capacity",
                 "Environmental", "Accessibility", "Quality Management"]

def filler_section(label, title, n_sub, style, P, C):
    s = STYLE[style]
    lines = [f"## Section {label}. {title}\n"]
    for i in range(1, n_sub + 1):
        paras = [rng.choice(s["filler"]).format(t=rng.choice(FILLER_TOPICS), P=P, C=C, d=rng.choice([5, 10, 15, 20]))
                 for _ in range(rng.randint(2, 4))]
        lines.append(f"**{label}.{i}** " + " ".join(paras) + "\n")
    return "\n".join(lines)

# ----------------------------------------------------------------------------- contract builder
def build_contract(idx, scale=1.0):
    # Re-seed per contract so a re-build with a different length scale keeps the same terms.
    global rng
    rng = random.Random(2026 + idx)
    ctype = rng.choice(list(CONTRACT_TYPES))
    T = CONTRACT_TYPES[ctype]
    style = rng.choice(["legalese", "plain", "british"])
    complexity = rng.choices(["low", "medium", "high"], weights=[3, 4, 4])[0]
    target_pages = rng.randint(10, 50)
    P_name, C_name = rng.choice(PROVIDERS), rng.choice(CUSTOMERS)
    P, C = T["p"], T["c"]
    sh, may = STYLE[style]["shall"], STYLE[style]["may"]
    law = rng.choice(LAWS if style != "british" else ["England and Wales", "Scotland"])
    health = ctype in ("MSA", "SaaS", "DPA") and rng.random() < 0.6     # adds a BAA/DPA exhibit that overrides breach notice
    eff_y, eff_m = rng.choice([2023, 2024, 2025]), rng.randint(1, 12)
    eff_date = fmt_date(eff_y, eff_m, 1, style)

    # --- original terms
    t = {
        "notice_days": rng.choice([30, 45, 60, 90]),
        "nonrenew_days": rng.choice([30, 60, 90]),
        "cure_days": rng.choice([10, 15, 30]),
        "breach_hours": rng.choice([48, 72]),
        "baa_hours": rng.choice([24, 36]),
        "credit_cap": rng.choice([10, 15, 20, 25]),
        "credit_per": rng.choice([2, 5, 10]),
        "uptime": rng.choice(["99.5", "99.9", "99.95"]),
        "fee_inc": rng.choice([3, 5, 7]),
        "term_months": rng.choice([12, 24, 36]),
        "fee": rng.choice([12000, 25000, 48000, 85000, 120000, 240000]),
        "tfc_months": rng.choice([3, 6, 12]),
        "liab_months": rng.choice([12, 24]),
        "special_mult": rng.choice([2, 3, 5]),
        "chronic_consec": rng.choice([2, 3]),
        "chronic_window": rng.choice([3, 4, 5]),
    }
    fee_str, sym = money(t["fee"], style)
    cur = dict(t)          # current terms after amendments
    amendments = []        # (title, [clauses])

    n_amend = {"low": 0, "medium": 1, "high": rng.choice([2, 3])}[complexity]
    amendable = ["notice_days", "credit_cap", "fee_inc", "term_months", "fee", "cure_days"]
    rng.shuffle(amendable)
    # high complexity: the same term is amended twice (stacked override) when there are 2+ amendments
    plan = []
    for a in range(n_amend):
        keys = amendable[a * 2:(a * 2) + rng.choice([1, 2])]
        plan.append(keys)
    if complexity == "high" and n_amend >= 2:
        plan[-1] = list(dict.fromkeys(plan[-1] + [plan[0][0]]))

    def amend_clause(key):
        old = cur[key]
        if key == "notice_days":
            new = rng.choice([v for v in (90, 120, 180) if v != old])
            txt = f"Section 15.3 is amended by deleting \"{num(old, style)} days\" and substituting \"{num(new, style)} days\"."
        elif key == "credit_cap":
            new = rng.choice([v for v in (30, 35, 40, 50) if v != old])
            txt = f"Exhibit B, paragraph B.5 is amended by replacing \"{old}%\" with \"{new}%\"."
        elif key == "fee_inc":
            new = rng.choice([v for v in (2, 3, 4) if v != old])
            txt = f"Section 7.4 is amended so that annual increases {sh} not exceed {new}%, irrespective of any index."
        elif key == "term_months":
            new = old + rng.choice([12, 24])
            txt = f"Section 15.1 is amended to extend the Initial Term to {num(new, style)} months."
        elif key == "fee":
            new = int(old * rng.choice([0.9, 0.94, 1.08]))
            txt = f"Exhibit A, paragraph A.1 is amended so that the {T['fee']} is {money(new, style)[0].replace('£', sym).replace('$', sym)}."
        else:  # cure_days
            new = rng.choice([v for v in (10, 20, 45, 60) if v != old])
            txt = f"Section 15.4(a) is amended by replacing the cure period with {num(new, style)} days."
        cur[key] = new
        return txt

    for a, keys in enumerate(plan, start=1):
        ay, am = add_months(eff_y, eff_m, 6 * a + rng.randint(0, 5))
        clauses = [amend_clause(k) for k in keys]
        amendments.append((f"AMENDMENT NO. {a} (Effective {fmt_date(ay, am, 1, style)})", clauses))

    # ----------------------------------------------------------------- core sections (with traps)
    core = {}
    whereas = ("\n\n**RECITALS**\n\nWHEREAS, " + P + f" is in the business of providing the services described herein; and\n\n"
               f"WHEREAS, {C} wishes to obtain such services on the terms set forth below;\n\n"
               "NOW, THEREFORE, in consideration of the mutual covenants herein, the Parties agree as follows:") \
        if STYLE[style]["whereas"] else ""
    core["intro"] = (f"# {T['title'].upper()}\n\nThis {T['title']} (the \"Agreement\") is entered into as of {eff_date} "
                     f"(the \"Effective Date\") by and between {P_name} (\"{P}\") and {C_name} (\"{C}\") "
                     f"{STYLE[style]['party_intro']}.{whereas}\n")

    restricted = rng.choice(["employee compensation data", "payment card data", "biometric data", "trade secret formulations"])
    core["defs"] = f"""## Section 1. Definitions

**1.1** "Business Day" means any day other than a Saturday, Sunday or public holiday in the place of performance.

**1.2** "Covered Data" means Personal Data, {'Protected Health Information, ' if health else ''}and any {C} Data designated as Restricted under Exhibit C.{' For clarity, Covered Data excludes De-identified Data.' if complexity != 'low' else ''}

**1.3** "Critical Incident" means a Severity 1 incident as described in Exhibit B, paragraph B.2.

**1.4** "Fees" means the amounts set out in Exhibit A, as adjusted under Section 7.4.

**1.5** "Liability Cap" means the amount determined under Section 13.2.

**1.6** "Special Damages Cap" means {num(t['special_mult'], style)} times the Liability Cap.

**1.7** "Termination for Convenience Fee" means the fee described in Exhibit A, paragraph A.6.
"""
    core["sla"] = f"""## Section 5. Service Levels

**5.1** {P} {sh} meet the Service Levels set out in Exhibit B.

**5.2** {P}'s sole liability for failing to meet a Service Level {sh} be the issue of Service Credits, except as provided in Section 15.4(c).

**5.3** Service Credits in any month {sh} not exceed the cap stated in Exhibit B, paragraph B.5.
"""
    core["fees"] = f"""## Section 7. Fees and Payment

**7.1** {C} {sh} pay the Fees set out in Exhibit A.

**7.2** Invoices are payable within {num(rng.choice([30, 45, 60]), style)} days of receipt.

**7.3** {C} {may} withhold amounts disputed in good faith by giving written notice describing the dispute.

**7.4** From the first anniversary of the Effective Date, {P} {may} increase the Fees once in each year by no more than the lesser of (a) the change in the consumer price index for the prior twelve months and (b) {t['fee_inc']}%.{' This Section 7.4 is subject to any Amendment.' if complexity == 'high' else ''}
"""
    core["security"] = f"""## Section 10. Data Protection and Security

**10.1** {P} {sh} implement the security controls described in Exhibit C.

**10.2** {P} {sh} notify {C} of any Security Breach involving Covered Data without undue delay and in any event within the period stated in Exhibit C, paragraph C.4.
""" + (f"""
**10.3** The Parties {sh} comply with the {'Business Associate Agreement' if ctype != 'DPA' else 'Data Processing Addendum'} attached as Exhibit D. Where this Agreement and Exhibit D conflict in respect of {'Protected Health Information' if ctype != 'DPA' else 'Personal Data'}, Exhibit D prevails.
""" if health else "")
    core["liability"] = f"""## Section 13. Limitation of Liability

**13.1** Except as provided in Section 13.3, neither Party {sh} be liable for indirect, incidental, consequential or punitive damages.

**13.2** Each Party's total liability under this Agreement {sh} not exceed the Fees paid or payable in the {num(t['liab_months'], style)} months before the event giving rise to the claim (the "Liability Cap").

**13.3** Sections 13.1 and 13.2 do not apply to (a) indemnification obligations, (b) gross negligence or wilful misconduct, or (c) breaches of Section 10 involving Covered Data, which are instead subject to the Special Damages Cap.
"""
    core["term"] = f"""## Section 15. Term and Termination

**15.1** This Agreement begins on the Effective Date and continues for {num(t['term_months'], style)} months (the "Initial Term").

**15.2** It then renews automatically for successive twelve-month periods unless either Party gives notice of non-renewal at least {num(t['nonrenew_days'], style)} days before the end of the current term.

**15.3** {C} {may} terminate this Agreement for convenience on {num(t['notice_days'], style)} days' written notice, subject to payment of the Termination for Convenience Fee.

**15.4** {C} {may} terminate immediately by written notice, without paying any Termination for Convenience Fee, if:
  (a) {P} materially breaches this Agreement and fails to cure within {num(t['cure_days'], style)} days of notice;
  (b) a Security Breach involving Covered Data occurs; or
  (c) a Chronic SLA Failure occurs, as defined in Exhibit B, paragraph B.6.
"""
    core["general"] = f"""## Section 18. General

**18.1** This Agreement is governed by the laws of {law}.

**18.2** Order of precedence: in the event of conflict, (1) any Amendment, {'(2) Exhibit D, (3)' if health else '(2)'} the body of this Agreement, then the remaining Exhibits, then any Order Form.

**18.3** This Agreement may be amended only by a written instrument signed by authorised representatives of both Parties.
"""
    core["exA"] = f"""# EXHIBIT A — FEES

**A.1** {T['fee'].capitalize()}: {fee_str} per month.

**A.2** Additional services: {money(rng.choice([150, 195, 225, 275]), style)[0]} per hour.

**A.6** Termination for Convenience Fee: an amount equal to the {T['fee']} that would have been payable for the lesser of (a) {num(t['tfc_months'], style)} months or (b) the remainder of the Initial Term.
"""
    core["exB"] = f"""# EXHIBIT B — SERVICE LEVELS

**B.1** Availability target: {t['uptime']}% monthly, excluding scheduled maintenance.

**B.2** Severity 1 means a complete loss of production service or any incident affecting Covered Data.

**B.3** Response times: Severity 1 — 15 minutes; Severity 2 — 2 hours; Severity 3 — 1 Business Day.

**B.4** Service Credit: {t['credit_per']}% of the monthly {T['fee']} for each 0.1% below the availability target.

**B.5** Service Credits {sh} not exceed {t['credit_cap']}% of the monthly {T['fee']} in any month.

**B.6** "Chronic SLA Failure" means failure to meet the availability target in (a) {num(t['chronic_consec'], style)} consecutive months or (b) any {num(t['chronic_window'], style)} months within a rolling twelve-month period.
"""
    core["exC"] = f"""# EXHIBIT C — SECURITY

**C.1** {P} {sh} maintain SOC 2 Type II {'and ISO 27001 ' if style == 'british' else ''}certification.

**C.2** Restricted {C} Data means {C} Data labelled "Restricted" in the data inventory, including {restricted}.

**C.3** Encryption: AES-256 at rest; TLS 1.2 or higher in transit.

**C.4** Security Breach notification: within {num(t['breach_hours'], style)} hours of discovery.
"""
    if health:
        core["exD"] = f"""# EXHIBIT D — {'BUSINESS ASSOCIATE AGREEMENT' if ctype != 'DPA' else 'DATA PROCESSING ADDENDUM'} (EXCERPT)

**D.1** {P} {sh} report any breach of {'Unsecured Protected Health Information' if ctype != 'DPA' else 'Personal Data'} to {C} within {num(t['baa_hours'], style)} hours of discovery.

**D.2** On termination, {P} {sh} return or destroy all such data within thirty days.
"""
    # --------------------------------------------------------------- assemble with filler to reach target length
    body_order = [("intro", None), ("defs", None)]
    numbered = {5: "sla", 7: "fees", 10: "security", 13: "liability", 15: "term", 18: "general"}
    titles = rng.sample(FILLER_TITLES, len(FILLER_TITLES))
    core_words = sum(len(v.split()) for v in core.values()) + sum(len(" ".join(c).split()) for _, c in amendments)
    fill_words = max(0, target_pages * WORDS_PER_PAGE - core_words)
    filler_secs = [n for n in range(2, 19) if n not in numbered] + ["A-Appendix", "B-Appendix", "C-Appendix"]
    per_sec = max(1, round(scale * fill_words / (len(filler_secs) * 160)))   # ~160 words per sub-clause

    parts, ti = [core["intro"], core["defs"]], 0
    for n in range(2, 19):
        if n in numbered:
            parts.append(core[numbered[n]])
        else:
            parts.append(filler_section(n, titles[ti % len(titles)], max(1, int(per_sec * rng.uniform(0.6, 1.4))), style, P, C)); ti += 1
    for ex in ["exA", "exB", "exC"] + (["exD"] if health else []):
        parts.append(core[ex])
        if ex in ("exA", "exB", "exC"):
            label = ex[-1] + "-Appendix"
            parts.append(filler_section(label, rng.choice(["Administration", "Measurement Methodology", "Programme Management"]),
                                        max(1, int(per_sec * rng.uniform(0.6, 1.4))), style, P, C))
    for title, clauses in amendments:
        parts.append(f"# {title}\n\n" + "\n\n".join(f"**{i}.** {c}" for i, c in enumerate(clauses, 1)) + "\n")
    md = "\n\n".join(parts)

    # --------------------------------------------------------------- answer key (computed from final terms)
    end_y, end_m = add_months(eff_y, eff_m, cur["term_months"])
    end_y, end_m = add_months(end_y, end_m, -1)
    tfc = cur["tfc_months"] * cur["fee"]
    amended = lambda k: cur[k] != t[k]
    qa = [
        ("How much notice is needed to terminate for convenience?",
         f"{cur['notice_days']} days" + (f" (amended from {t['notice_days']})" if amended("notice_days") else ""),
         ["15.3"] + (["Amendment"] if amended("notice_days") else [])),
        ("What is the maximum monthly service credit?",
         f"{cur['credit_cap']}% of the monthly fee" + (f" (amended from {t['credit_cap']}%)" if amended("credit_cap") else ""),
         ["5.3", "Exhibit B.5"] + (["Amendment"] if amended("credit_cap") else [])),
        ("What is the maximum annual fee increase?",
         (f"{cur['fee_inc']}% regardless of the index (amended)" if amended("fee_inc") else f"the lesser of CPI change and {t['fee_inc']}%"),
         ["7.4"] + (["Amendment"] if amended("fee_inc") else [])),
        ("When does the Initial Term end?",
         f"{cur['term_months']} months from {eff_date}, i.e. end of {fmt_date(end_y, end_m, 1, 'plain').split(' 1,')[0]} {end_y}",
         ["15.1", "Preamble"] + (["Amendment"] if amended("term_months") else [])),
        ("What would the termination for convenience fee be (assuming more than the fee period remains in the Initial Term)?",
         f"{cur['tfc_months']} months x {sym}{cur['fee']:,} = {sym}{tfc:,}",
         ["1.7", "Exhibit A.6", "Exhibit A.1"] + (["Amendment"] if amended("fee") else [])),
        ("How long is the cure period before the customer can terminate for material breach?",
         f"{cur['cure_days']} days" + (f" (amended from {t['cure_days']})" if amended("cure_days") else ""),
         ["15.4(a)"] + (["Amendment"] if amended("cure_days") else [])),
        ("Can the customer terminate without a fee if uptime targets are repeatedly missed?",
         f"Yes - Chronic SLA Failure ({cur['chronic_consec']} consecutive months or {cur['chronic_window']} months in a rolling 12) allows termination under 15.4(c) with no fee",
         ["5.2", "15.4(c)", "Exhibit B.6"]),
        ("Is liability for a breach of Covered Data limited to the standard liability cap?",
         f"No - it is subject to the Special Damages Cap, {cur['special_mult']}x the Liability Cap ({cur['liab_months']} months of fees)",
         ["13.2", "13.3(c)", "1.6"]),
        (f"Does a breach involving {restricted} count as a breach of Covered Data?",
         "Yes - it is Restricted data under Exhibit C.2, which is included in Covered Data",
         ["1.2", "Exhibit C.2"]),
        ("How quickly must a security breach be reported?",
         (f"{cur['baa_hours']} hours for {'PHI' if ctype != 'DPA' else 'personal data'} under Exhibit D (which prevails); {cur['breach_hours']} hours for other Covered Data"
          if health else f"{cur['breach_hours']} hours"),
         ["10.2", "Exhibit C.4"] + (["10.3", "Exhibit D.1", "18.2"] if health else [])),
    ]
    if amendments:
        qa.append(("Which document controls if an Amendment conflicts with the main body?", "The Amendment (Section 18.2)", ["18.2"]))

    meta = {"contract_id": f"contract_{idx:02d}", "type": ctype, "title": T["title"], "style": style,
            "complexity": complexity, "amendments": len(amendments), "has_exhibit_d": health,
            "target_pages": target_pages, "words": len(md.split()), "est_pages": round(len(md.split()) / WORDS_PER_PAGE),
            "provider": P_name, "customer": C_name, "effective_date": eff_date}
    return md, meta, [{"question": q, "expected": a, "sources": s} for q, a, s in qa]

# ----------------------------------------------------------------------------- PDF rendering
def write_pdf(md, path):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from reportlab.pdfbase import pdfmetrics  # noqa: F401  (Helvetica covers £ and —)
    styles = getSampleStyleSheet()
    story = []
    for block in md.split("\n\n"):
        b = block.strip()
        if not b:
            continue
        b = b.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        b = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", b).replace("\n", "<br/>")
        if b.startswith("# "):
            story.append(Paragraph(b[2:], styles["Title"]))
        elif b.startswith("## "):
            story.append(Paragraph(b[3:], styles["Heading2"]))
        else:
            story.append(Paragraph(b, styles["BodyText"]))
        story.append(Spacer(1, 6))
    SimpleDocTemplate(str(path), pagesize=letter).build(story)

def main():
    from pypdf import PdfReader
    manifest, key = [], {}
    PDF_OUT.mkdir(parents=True, exist_ok=True); MD_OUT.mkdir(parents=True, exist_ok=True)
    for i in range(1, N_CONTRACTS + 1):
        scale = 1.0
        for attempt in range(12):   # calibrate boilerplate volume to the target PDF page count
            md, meta, qa = build_contract(i, scale)
            cid = meta["contract_id"]
            write_pdf(md, PDF_OUT / f"{cid}.pdf")
            pages = len(PdfReader(str(PDF_OUT / f"{cid}.pdf")).pages)
            in_range = 10 <= pages <= 50
            if in_range and (abs(pages - meta["target_pages"]) <= 2 or attempt >= 6):
                break
            scale *= meta["target_pages"] / pages
        (MD_OUT / f"{cid}.md").write_text(md)
        meta["pdf_pages"] = pages
        meta["est_pages"] = pages
        manifest.append(meta)
        key[cid] = {"meta": meta, "questions": qa}
        print(f"{cid}: {meta['type']:<10} {meta['style']:<8} {meta['complexity']:<6} amend={meta['amendments']} "
              f"exD={str(meta['has_exhibit_d'])[0]} pages={meta['pdf_pages']}")
    with open(OUT / "contracts_manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(manifest[0].keys()))
        w.writeheader(); w.writerows(manifest)
    (OUT / "contracts_answer_key.json").write_text(json.dumps(key, indent=1))
    print(f"\nWrote {N_CONTRACTS} contracts, contracts_manifest.csv, contracts_answer_key.json")

if __name__ == "__main__":
    main()
