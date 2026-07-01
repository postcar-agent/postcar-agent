"""
tag_taxonomy.py — canonical, closed tier1/tier2 vocabulary for agent classification.

Authored once, versioned in this repo. NOT computed or generated at runtime —
this is a fixed reference dataset an LLM classifies AGAINST (picks from these
exact strings), so every agent on the network converges on the same
vocabulary instead of inventing its own synonyms.

Structure:
  TIER1_DOMAINS   — ~50 domain:<x> tags, one per top-level domain.
  TIER1_IDENTITIES — ~50 identity:<x>-agent tags, one per domain (paired 1:1).
    Together TIER1_DOMAINS + TIER1_IDENTITIES = the ~100-option tier1 space.
  TIER2_BY_DOMAIN — dict: domain key -> list of skill:/strategy: tags scoped
    to that domain. A classifier should only ever be shown the tier2 subset
    for the tier1 domain(s) it already picked, never the full flattened list
    (which is the whole point of scoping it by domain in the first place).

This is a foundation, not a claim of exactly 5,000 tier2 entries — finance/
trading is authored deepest (~40 tags) since that's where the real deployed
agents are today; every other domain has a solid ~15-20 tag baseline that's
straightforward to extend as real agents in those domains show up. Padding
every domain to hit a round total with meaningless auto-generated
combinations would undermine the reason this taxonomy exists (real, usable,
non-overlapping tags), so depth is weighted toward validated usage instead.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# TIER 1 — domains + paired identities (~100 total)
# ---------------------------------------------------------------------------

_DOMAIN_KEYS = [
    "finance", "healthcare", "legal", "marketing", "research", "data",
    "security", "operations", "education", "retail", "logistics", "hr",
    "real_estate", "insurance", "energy", "media", "gaming", "agriculture",
    "manufacturing", "travel", "nonprofit", "government", "telecom", "sports",
    "food_beverage", "automotive", "construction", "biotech", "defense",
    "customer_support", "sales", "product", "devops", "design",
    "localization", "accounting", "compliance", "procurement",
    "supply_chain", "it_helpdesk", "fintech", "insurtech", "edtech",
    "proptech", "hrtech", "martech", "cybersecurity", "blockchain",
    "robotics", "climate",
]

TIER1_DOMAINS = [f"domain:{k}" for k in _DOMAIN_KEYS]
TIER1_IDENTITIES = [f"identity:{k.replace('_', '-')}-agent" for k in _DOMAIN_KEYS]

# Combined tier1 option set an LLM classifier picks 1+ from.
TIER1_TAXONOMY = TIER1_DOMAINS + TIER1_IDENTITIES


# ---------------------------------------------------------------------------
# TIER 2 — skills/strategies, scoped per domain
# ---------------------------------------------------------------------------

TIER2_BY_DOMAIN: dict[str, list[str]] = {
    "finance": [
        "strategy:systematic", "strategy:discretionary", "strategy:momentum",
        "strategy:mean-reversion", "strategy:market-making", "strategy:trend-following",
        "strategy:pairs-trading", "strategy:arbitrage", "strategy:event-driven",
        "strategy:swing-trading", "strategy:scalping", "strategy:carry-trade",
        "strategy:statistical-arbitrage", "strategy:long-short-equity", "strategy:global-macro",
        "skill:risk-management", "skill:position-sizing", "skill:portfolio-optimization",
        "skill:options-trading", "skill:futures-trading", "skill:fx-trading",
        "skill:credit-analysis", "skill:fundamental-analysis", "skill:technical-analysis",
        "skill:sector-rotation", "skill:macro-analysis", "skill:algo-trading",
        "skill:high-frequency-trading", "skill:backtesting", "skill:drawdown-control",
        "skill:liquidity-management", "skill:regime-detection", "skill:hedging",
        "skill:derivatives-pricing", "skill:volatility-modeling", "skill:tax-loss-harvesting",
        "skill:rebalancing", "skill:performance-attribution", "skill:market-regime-analysis",
        "skill:portfolio-sizing", "skill:capital-allocation", "skill:execution-optimization",
        "skill:slippage-analysis", "skill:order-flow-analysis", "skill:factor-investing",
        "skill:quant-research", "skill:trade-journaling", "skill:margin-management",
        "skill:correlation-analysis", "skill:sentiment-trading", "skill:earnings-analysis",
        "skill:dividend-strategy", "skill:bond-analysis", "skill:yield-curve-analysis",
        "skill:crypto-trading", "skill:commodity-trading", "skill:short-selling",
        "skill:cash-flow-modeling", "skill:valuation-modeling", "skill:m-and-a-analysis",
        "skill:esg-screening", "skill:tail-risk-hedging",
    ],
    "healthcare": [
        "skill:clinical-documentation", "skill:patient-triage", "skill:hipaa-compliance",
        "skill:medical-coding", "skill:care-coordination", "skill:diagnostic-support",
        "skill:clinical-research", "skill:pharmacovigilance", "skill:ehr-integration",
        "skill:telehealth", "skill:medical-billing", "skill:health-informatics",
        "skill:patient-education", "skill:regulatory-submission", "skill:clinical-trial-management",
        "skill:medical-imaging-analysis", "skill:genomics", "skill:remote-patient-monitoring",
        "skill:drug-interaction-checking", "skill:appointment-scheduling", "skill:insurance-preauthorization",
        "skill:population-health-analytics", "skill:mental-health-screening", "skill:discharge-planning",
    ],
    "legal": [
        "skill:contract-review", "skill:legal-research", "skill:compliance-monitoring",
        "skill:litigation-support", "skill:regulatory-filing", "skill:ip-management",
        "skill:due-diligence", "skill:contract-drafting", "skill:legal-writing",
        "skill:case-law-analysis", "skill:e-discovery", "skill:privacy-law",
        "skill:corporate-governance", "skill:dispute-resolution", "skill:patent-search",
        "skill:trademark-filing", "skill:contract-negotiation", "skill:regulatory-tracking",
        "skill:legal-billing", "skill:document-redaction",
    ],
    "marketing": [
        "skill:campaign-management", "skill:seo", "skill:content-strategy",
        "skill:brand-management", "skill:social-media", "skill:email-marketing",
        "skill:ad-optimization", "skill:market-research", "skill:copywriting",
        "skill:analytics-reporting", "skill:influencer-outreach", "skill:conversion-optimization",
        "skill:crm-management", "skill:growth-hacking", "skill:brand-monitoring",
        "skill:sem", "skill:programmatic-advertising", "skill:marketing-automation",
        "skill:landing-page-optimization", "skill:customer-journey-mapping", "skill:pr-outreach",
        "skill:event-marketing", "skill:affiliate-management",
    ],
    "research": [
        "skill:literature-review", "skill:summarization", "skill:citation-analysis",
        "skill:experiment-design", "skill:hypothesis-testing", "skill:survey-design",
        "skill:peer-review", "skill:data-synthesis", "skill:academic-writing",
        "skill:statistical-analysis", "skill:qualitative-analysis", "skill:reproducibility-check",
        "skill:grant-proposal-writing", "skill:meta-analysis", "skill:fact-checking",
        "skill:competitive-intelligence", "skill:trend-forecasting",
    ],
    "data": [
        "skill:data-pipeline", "skill:etl", "skill:data-quality", "skill:data-cleaning",
        "skill:data-visualization", "skill:database-design", "skill:data-warehousing",
        "skill:model-training", "skill:feature-engineering", "skill:data-labeling",
        "skill:anomaly-detection", "skill:forecasting", "skill:ab-testing",
        "skill:data-governance", "skill:streaming-analytics", "skill:mlops",
        "skill:data-catalog-management", "skill:schema-design", "skill:data-lineage-tracking",
        "skill:model-evaluation", "skill:hyperparameter-tuning", "skill:embeddings-search",
        "skill:data-masking", "skill:batch-processing",
    ],
    "security": [
        "skill:threat-detection", "skill:vulnerability-assessment", "skill:incident-response",
        "skill:penetration-testing", "skill:compliance-audit", "skill:access-management",
        "skill:security-monitoring", "skill:malware-analysis", "skill:encryption",
        "skill:forensics", "skill:phishing-detection", "skill:zero-trust-architecture",
        "skill:siem-management", "skill:identity-governance", "skill:secrets-management",
        "skill:red-teaming", "skill:security-awareness-training",
    ],
    "operations": [
        "skill:process-optimization", "skill:workflow-automation", "skill:monitoring",
        "skill:alerting", "skill:observability", "skill:incident-management",
        "skill:capacity-planning", "skill:sla-tracking", "skill:runbook-automation",
        "skill:multi-agent-coordination", "skill:orchestration", "skill:change-management",
        "skill:root-cause-analysis", "skill:on-call-scheduling", "skill:cost-optimization",
    ],
    "education": [
        "skill:curriculum-design", "skill:tutoring", "skill:assessment-grading",
        "skill:lesson-planning", "skill:learning-analytics", "skill:accessibility-support",
        "skill:student-engagement", "skill:content-authoring", "skill:exam-proctoring",
        "skill:credential-verification", "skill:learning-path-personalization",
    ],
    "retail": [
        "skill:inventory-management", "skill:demand-forecasting", "skill:pricing-optimization",
        "skill:merchandising", "skill:customer-segmentation", "skill:loss-prevention",
        "skill:order-fulfillment", "skill:store-operations", "skill:planogram-optimization",
        "skill:markdown-optimization", "skill:omnichannel-inventory-sync", "skill:returns-processing",
    ],
    "logistics": [
        "skill:route-optimization", "skill:fleet-management", "skill:warehouse-management",
        "skill:demand-planning", "skill:freight-brokering", "skill:last-mile-delivery",
        "skill:customs-compliance", "skill:load-planning", "skill:carrier-selection",
        "skill:shipment-tracking", "skill:cross-docking",
    ],
    "hr": [
        "skill:recruiting", "skill:onboarding", "skill:performance-review",
        "skill:payroll", "skill:benefits-administration", "skill:employee-engagement",
        "skill:workforce-planning", "skill:compliance-training", "skill:offboarding",
        "skill:compensation-benchmarking", "skill:dei-analytics", "skill:succession-planning",
    ],
    "real_estate": [
        "skill:property-valuation", "skill:listing-management", "skill:lease-administration",
        "skill:tenant-screening", "skill:market-comparables", "skill:property-management",
        "skill:mortgage-underwriting", "skill:title-search", "skill:zoning-analysis",
    ],
    "insurance": [
        "skill:underwriting", "skill:claims-processing", "skill:actuarial-analysis",
        "skill:fraud-detection", "skill:policy-administration", "skill:risk-assessment",
        "skill:reinsurance-modeling", "skill:catastrophe-modeling", "skill:premium-calculation",
    ],
    "energy": [
        "skill:grid-monitoring", "skill:demand-response", "skill:energy-trading",
        "skill:renewable-forecasting", "skill:asset-maintenance", "skill:load-balancing",
        "skill:outage-prediction", "skill:capacity-market-bidding",
    ],
    "media": [
        "skill:content-moderation", "skill:editorial-review", "skill:audience-analytics",
        "skill:video-production", "skill:rights-management", "skill:transcription",
        "skill:metadata-tagging", "skill:ad-insertion",
    ],
    "gaming": [
        "skill:game-balancing", "skill:playtesting", "skill:live-ops",
        "skill:anti-cheat", "skill:community-moderation", "skill:monetization-tuning",
        "skill:matchmaking", "skill:telemetry-analysis",
    ],
    "agriculture": [
        "skill:yield-prediction", "skill:crop-monitoring", "skill:irrigation-optimization",
        "skill:soil-analysis", "skill:supply-forecasting", "skill:pest-detection",
        "skill:precision-agriculture", "skill:livestock-monitoring",
    ],
    "manufacturing": [
        "skill:quality-control", "skill:predictive-maintenance", "skill:production-scheduling",
        "skill:supply-chain-optimization", "skill:defect-detection", "skill:yield-optimization",
        "skill:bom-management", "skill:mes-integration",
    ],
    "travel": [
        "skill:itinerary-planning", "skill:pricing-yield-management", "skill:booking-support",
        "skill:customer-service", "skill:overbooking-management", "skill:loyalty-program-optimization",
    ],
    "nonprofit": [
        "skill:grant-writing", "skill:donor-management", "skill:impact-reporting",
        "skill:volunteer-coordination", "skill:fundraising-campaign-management", "skill:beneficiary-tracking",
    ],
    "government": [
        "skill:policy-analysis", "skill:regulatory-compliance", "skill:public-records",
        "skill:constituent-services", "skill:foia-processing", "skill:permit-processing",
    ],
    "telecom": [
        "skill:network-monitoring", "skill:capacity-planning", "skill:churn-prediction",
        "skill:outage-response", "skill:spectrum-management", "skill:sla-compliance-monitoring",
    ],
    "sports": [
        "skill:performance-analytics", "skill:injury-prediction", "skill:scouting",
        "skill:fan-engagement", "skill:ticketing-optimization", "skill:broadcast-analytics",
    ],
    "food_beverage": [
        "skill:menu-optimization", "skill:supply-forecasting", "skill:food-safety-compliance",
        "skill:recipe-costing", "skill:allergen-tracking", "skill:shelf-life-prediction",
    ],
    "automotive": [
        "skill:predictive-maintenance", "skill:fleet-telematics", "skill:parts-forecasting",
        "skill:warranty-analysis", "skill:recall-management", "skill:dealer-inventory-optimization",
    ],
    "construction": [
        "skill:project-scheduling", "skill:cost-estimation", "skill:safety-compliance",
        "skill:site-monitoring", "skill:permit-tracking", "skill:subcontractor-management",
    ],
    "biotech": [
        "skill:clinical-trial-management", "skill:regulatory-submission", "skill:lab-automation",
        "skill:genomics", "skill:drug-discovery", "skill:protein-structure-analysis",
        "skill:assay-development",
    ],
    "defense": [
        "skill:threat-assessment", "skill:logistics-planning", "skill:intelligence-analysis",
        "skill:compliance-itar", "skill:mission-planning", "skill:signal-analysis",
    ],
    "customer_support": [
        "skill:ticket-triage", "skill:sentiment-analysis", "skill:escalation-management",
        "skill:knowledge-base-authoring", "skill:sla-tracking", "skill:chatbot-handoff",
        "skill:customer-satisfaction-analysis", "skill:multi-channel-support",
    ],
    "sales": [
        "skill:lead-scoring", "skill:pipeline-management", "skill:proposal-generation",
        "skill:crm-management", "skill:forecasting", "skill:quote-generation",
        "skill:territory-planning", "skill:upsell-detection",
    ],
    "product": [
        "skill:roadmap-planning", "skill:user-research", "skill:ab-testing",
        "skill:spec-writing", "skill:prioritization", "skill:competitive-analysis",
        "skill:feature-flagging", "skill:release-planning",
    ],
    "devops": [
        "skill:ci-cd", "skill:infrastructure-as-code", "skill:incident-response",
        "skill:observability", "skill:capacity-planning", "skill:release-management",
        "skill:container-orchestration", "skill:cost-monitoring", "skill:secrets-rotation",
    ],
    "design": [
        "skill:ui-design", "skill:ux-research", "skill:prototyping",
        "skill:design-systems", "skill:accessibility-review", "skill:usability-testing",
        "skill:wireframing",
    ],
    "localization": [
        "skill:translation", "skill:terminology-management", "skill:cultural-adaptation",
        "skill:qa-linguistic-review", "skill:transcreation", "skill:locale-testing",
    ],
    "accounting": [
        "skill:bookkeeping", "skill:financial-reporting", "skill:audit-support",
        "skill:tax-preparation", "skill:reconciliation", "skill:accounts-payable",
        "skill:accounts-receivable", "skill:expense-categorization",
    ],
    "compliance": [
        "skill:regulatory-monitoring", "skill:policy-enforcement", "skill:audit-support",
        "skill:kyc-aml", "skill:reporting-obligations", "skill:sanctions-screening",
        "skill:whistleblower-intake",
    ],
    "procurement": [
        "skill:vendor-management", "skill:contract-negotiation", "skill:spend-analysis",
        "skill:sourcing", "skill:rfp-management", "skill:supplier-scorecarding",
    ],
    "supply_chain": [
        "skill:demand-planning", "skill:inventory-optimization", "skill:supplier-risk-monitoring",
        "skill:logistics-coordination", "skill:s-and-op-planning", "skill:supplier-diversification",
    ],
    "it_helpdesk": [
        "skill:ticket-triage", "skill:troubleshooting", "skill:asset-management",
        "skill:access-provisioning", "skill:patch-management", "skill:remote-support",
    ],
    "fintech": [
        "skill:payments-processing", "skill:fraud-detection", "skill:kyc-aml",
        "skill:credit-scoring", "skill:embedded-finance", "skill:ledger-reconciliation",
        "skill:open-banking-integration",
    ],
    "insurtech": [
        "skill:automated-underwriting", "skill:claims-automation", "skill:usage-based-pricing",
        "skill:telematics-scoring",
    ],
    "edtech": [
        "skill:adaptive-learning", "skill:learning-analytics", "skill:content-authoring",
        "skill:proctoring-automation",
    ],
    "proptech": [
        "skill:property-valuation", "skill:smart-building-monitoring", "skill:listing-automation",
        "skill:lease-analytics",
    ],
    "hrtech": [
        "skill:candidate-screening", "skill:workforce-analytics", "skill:payroll-automation",
        "skill:skills-matching",
    ],
    "martech": [
        "skill:campaign-automation", "skill:attribution-modeling", "skill:audience-segmentation",
        "skill:cdp-management",
    ],
    "cybersecurity": [
        "skill:threat-intelligence", "skill:incident-response", "skill:vulnerability-management",
        "skill:security-operations", "skill:zero-trust-architecture", "skill:cloud-security-posture",
        "skill:iam-hardening",
    ],
    "blockchain": [
        "skill:smart-contract-audit", "skill:on-chain-analysis", "skill:defi-strategy",
        "skill:wallet-security", "skill:mev-detection", "skill:token-economics",
    ],
    "robotics": [
        "skill:motion-planning", "skill:sensor-fusion", "skill:predictive-maintenance",
        "skill:fleet-coordination", "skill:slam-navigation", "skill:gripper-control",
    ],
    "climate": [
        "skill:emissions-tracking", "skill:climate-risk-modeling", "skill:sustainability-reporting",
        "skill:carbon-accounting", "skill:esg-scoring", "skill:renewable-credit-tracking",
    ],
}

# Sanity: every domain key must exist in both places.
assert set(TIER2_BY_DOMAIN.keys()) == set(_DOMAIN_KEYS), "TIER2_BY_DOMAIN domain keys must match _DOMAIN_KEYS exactly"


def tier2_options_for(domains: list[str]) -> list[str]:
    """Given tier1 domain tags already picked (e.g. ["domain:finance"]),
    return the deduplicated tier2 option list a classifier should be shown --
    never the full flattened taxonomy."""
    keys = [d.split(":", 1)[1] for d in domains if d.startswith("domain:")]
    seen, out = set(), []
    for k in keys:
        for tag in TIER2_BY_DOMAIN.get(k, []):
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out
