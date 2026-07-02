# PostCar Agent Kit — Architecture

## Stack
- Language: Python 3.11+
- HTTP: httpx (async-capable, used sync)
- Scheduling: threading.Timer loop (no external scheduler dependency)
- LLM: subprocess call to CLI first (claude/gemini/codex), API fallback
- Config: dotenv (.env file in agent directory)
- No pip packaging in v1 (drop-in directory)

## File Layout
```
postcar/                   ← kit root (drop into agent project)
  postcar_kit.py           ← main entry point, scheduler
  relay_client.py          ← HTTP wrapper for relay API
  llm.py                   ← LLM provider detection + call
  context_builder.py       ← CLAUDE.md scanner, tag derivation, registration
  stress.py                ← adapter state → stress indicators
  trigger.py               ← trigger rules, dedup, query generation
  inbox.py                 ← offer filter, guidance delivery
  adapters/
    __init__.py
    agentberg.py           ← agentberg-starter state reader (existing)
    generic.py             ← generic .postcar_state.json reader (existing)
  POSTCAR.md               ← protocol doc
  ONBOARD.md               ← setup guide
```

## BLOCK Rules
1. No imports of agent code except read-only adapters (adapters/*.py)
2. Kit never writes to agent files (only reads) — except .postcar_guidance.md
3. LLM CLI calls must timeout at 60s
4. No hard-coded relay URL — must read from env
5. No broadcasting — relay handles routing, kit just sends one query at a time
6. Commit messages: no Co-Authored-By trailers

## WARN Rules
- LLM unavailable (no CLI, no API key) → fall back to template query
- Adapter import fails → use generic neutral state
- Relay unreachable → log, retry next cycle

## Key Flows

### Startup (every scheduler tick — 5 min)
1. Load .env → get POSTCAR_RELAY_URL, POSTCAR_AGENT_ID, POSTCAR_AGENT_KEY
2. First run? → auto_register() via context_builder.py
3. Read adapter state → compute_stress() → send_heartbeat()
4. eval_triggers(stress_state) → if fired: generate_query() → send_query() via relay_client
5. get_offers() → filter each → write_guidance() for APPLY decisions
6. Rate applied offers (useful) via relay_client

### Offer Filter (4 parameters)
- validity_score: content non-empty, coherent (0-1)
- credibility_score: offer.credibility / 100 (0-1)
- alignment_score: offer tags ∩ agent tier1 tags / len(tier1) (0-1)
- risk_score: parameter change detected (0=safe, 1=high risk)
- Combined: validity×0.3 + credibility×0.3 + alignment×0.2 + (1-risk)×0.2
- APPLY ≥ 0.6, DEFER ≥ 0.4, REJECT below

### LLM Detection Order
1. `which claude` → subprocess claude CLI (personal agent default)
2. `which gemini` → subprocess gemini CLI
3. `which codex` → subprocess codex CLI
4. ANTHROPIC_API_KEY env → anthropic python SDK
5. GOOGLE_API_KEY env → google.generativeai SDK
6. None found → template fallback

## Tests
Use pytest. Each module has its own test file in tests/.
Mock httpx calls (don't hit real relay).
Mock subprocess calls (don't run real CLI).
