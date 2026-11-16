"""Default evaluation set (29 cases).

Document questions are checked with keywords; numeric questions are checked against a *gold SQL* query that
is executed on the live database, so the expected value can never drift out of sync with the data.
"""
from __future__ import annotations

REV = "ROUND(SUM(revenue), 2)"

CASES: list[dict] = [
    # ---------------------------------------------------------------- document Q&A
    {"id": "d01", "category": "doc_qa", "question": "What is the dosage of Zentrovax for a 15 kg child?", "contains_any": ["100 mg"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d02", "category": "doc_qa", "question": "What is the maximum daily dose of Zentrovax?", "contains_any": ["600 mg"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d03", "category": "doc_qa", "question": "Can Zentrovax be given to a 4-month-old infant?", "contains_any": ["not recommended", "6 months"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d04", "category": "doc_qa", "question": "How should the Zentrovax dose be adjusted for a patient with an eGFR below 30?", "contains_any": ["halve", "50%", "half"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d05", "category": "doc_qa", "question": "What should be done if a dose of Zentrovax is missed?", "contains_any": ["4 hours"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d06", "category": "doc_qa", "question": "Within how long must a serious adverse event be reported to the sponsor?", "contains_any": ["24 hours"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d07", "category": "doc_qa", "question": "At what temperature must Zentrovax be stored?", "contains_any": ["2 to 8"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d08", "category": "doc_qa", "question": "What happens when a temperature excursion lasts longer than 30 minutes?", "contains_any": ["quarantine", "hold"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d09", "category": "doc_qa", "question": "Within how many hours must the regulator be notified of a confirmed data breach?", "contains_any": ["72 hours"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d10", "category": "doc_qa", "question": "Who must approve a discount above 15%?", "contains_any": ["regional director"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d11", "category": "doc_qa", "question": "How long are essential clinical trial records retained?", "contains_any": ["15 years"], "expect_tool": "search_docs", "needs_citation": True},
    {"id": "d12", "category": "doc_qa", "question": "How often do the temperature loggers record a reading?", "contains_any": ["15 minutes"], "expect_tool": "search_docs", "needs_citation": True},
    # ---------------------------------------------------------------- SQL / data
    {"id": "s01", "category": "sql", "question": "What was the total revenue in 2025?", "gold_sql": f"SELECT {REV} FROM sales WHERE year = 2025", "expect_tool": "run_sql"},
    {"id": "s02", "category": "sql", "question": "How many orders were placed in 2026?", "gold_sql": "SELECT COUNT(*) FROM sales WHERE year = 2026", "expect_tool": "run_sql"},
    {"id": "s03", "category": "sql", "question": "Which region had the highest revenue in 2025?", "gold_sql": f"SELECT region, {REV} AS r FROM sales WHERE year = 2025 GROUP BY region ORDER BY r DESC LIMIT 1", "expect_tool": "run_sql"},
    {"id": "s04", "category": "sql", "question": "Which product sold the most units in 2025?", "gold_sql": "SELECT product, SUM(quantity) AS u FROM sales WHERE year = 2025 GROUP BY product ORDER BY u DESC LIMIT 1", "expect_tool": "run_sql"},
    {"id": "s05", "category": "sql", "question": "How many units of Zentrovax were sold in 2026?", "gold_sql": "SELECT SUM(quantity) FROM sales WHERE year = 2026 AND product = 'Zentrovax'", "expect_tool": "run_sql"},
    {"id": "s06", "category": "sql", "question": "What was the average discount given in 2025?", "gold_sql": "SELECT ROUND(AVG(discount_pct), 2) FROM sales WHERE year = 2025", "expect_tool": "run_sql"},
    {"id": "s07", "category": "sql", "question": "How many accounts are in the East region?", "gold_sql": "SELECT COUNT(*) FROM accounts WHERE region = 'East'", "expect_tool": "run_sql"},
    {"id": "s08", "category": "sql", "question": "Which customer account generated the most revenue in 2026?", "gold_sql": f"SELECT account, {REV} AS r FROM sales WHERE year = 2026 GROUP BY account ORDER BY r DESC LIMIT 1", "expect_tool": "run_sql"},
    {"id": "s09", "category": "sql", "question": "What was the revenue in the West region in Q2 2026?", "gold_sql": f"SELECT {REV} FROM sales WHERE quarter = '2026-Q2' AND region = 'West'", "expect_tool": "run_sql"},
    {"id": "s10", "category": "sql", "question": "Which region had the biggest sales drop last quarter?", "gold_sql": "SELECT region FROM sales GROUP BY region ORDER BY SUM(CASE WHEN quarter = '2026-Q3' THEN revenue ELSE 0 END) - SUM(CASE WHEN quarter = '2026-Q2' THEN revenue ELSE 0 END) ASC LIMIT 1", "expect_tool": "run_sql"},
    # ---------------------------------------------------------------- needs both sources
    {"id": "m01", "category": "multi", "question": "What is the maximum daily dose of Zentrovax, and how many units of Zentrovax were sold in 2026?", "contains_any": ["600 mg"], "gold_sql": "SELECT SUM(quantity) FROM sales WHERE year = 2026 AND product = 'Zentrovax'", "expect_tool": "search_docs", "expect_tool2": "run_sql", "needs_citation": True},
    {"id": "m02", "category": "multi", "question": "Which approval does a discount above 15% need, and what was the average discount given in 2025?", "contains_any": ["regional director"], "gold_sql": "SELECT ROUND(AVG(discount_pct), 2) FROM sales WHERE year = 2025", "expect_tool": "search_docs", "expect_tool2": "run_sql", "needs_citation": True},
    {"id": "m03", "category": "multi", "question": "At what temperature must Zentrovax be stored, and how many orders were placed in 2025?", "contains_any": ["2 to 8"], "gold_sql": "SELECT COUNT(*) FROM sales WHERE year = 2025", "expect_tool": "search_docs", "expect_tool2": "run_sql", "needs_citation": True},
    # ---------------------------------------------------------------- should abstain
    {"id": "u01", "category": "unanswerable", "question": "What is the recommended dose of Neurazen for adults?", "expect_abstain": True},
    {"id": "u02", "category": "unanswerable", "question": "What is the CEO's salary?", "expect_abstain": True},
    {"id": "u03", "category": "unanswerable", "question": "What is the capital of France?", "expect_abstain": True},
    {"id": "u04", "category": "unanswerable", "question": "What was the total revenue in 2031?", "expect_abstain": True},
]

CATEGORY_LABELS = {
    "doc_qa": "Document Q&A",
    "sql": "Data questions",
    "multi": "Needs both",
    "unanswerable": "Should say 'I don't know'",
}
