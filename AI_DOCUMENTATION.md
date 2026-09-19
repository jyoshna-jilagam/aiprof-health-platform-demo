# AI Documentation

## Model / provider
Optional adapter to the Anthropic Messages API (`claude-sonnet-4-6`), used **only** for
structured intent extraction (specialty routing category, action, choice index). No API key
is required to run the app: with no key, or on any API error/timeout, the agent transparently
falls back to a deterministic keyword/regex engine, and the UI labels the reply
`(deterministic fallback - no AI key configured)` so the evaluator can see which path ran.

## Why intent-only
Per the safety boundary in the PRD, the LLM must never make clinical or business decisions.
It is prompted to return only administrative routing JSON:
```json
{"specialty": "orthopedics", "action": "search", "choice_index": null}
```
That JSON is fed into the same `capabilities.py` functions the fallback engine calls — so
availability, authorization, and booking logic is 100% deterministic and LLM-independent
regardless of which intent path produced the request.

## Prompts
Single system-style prompt (see `ai_agent._llm_extract`): instructs the model to act as an
"administrative intent extractor", explicitly forbidding diagnosis, and to return JSON only.

## Tools / capabilities available to the agent
`search_hospitals, search_doctors, check_availability, lookup_patient, get_appointment,
create_appointment, reschedule_appointment, cancel_appointment, get_questionnaire,
submit_questionnaire, send_notification, start_workflow, verify_external_appointment,
synchronize_state, transfer_to_human` — all in `capabilities.py`. The agent calls these
functions directly (in-process); it does not have SQL or EHR access.

## Voice implementation
Browser-based: `SpeechRecognition` (Web Speech API) for speech-to-text feeds the same text
input used by typed messages; `speechSynthesis` reads the agent's reply aloud. If the browser
doesn't support the API, the mic button is disabled with a message and the text input remains
fully functional — voice is never a hard requirement to use the app.

## Fallback behavior
The deterministic engine (`SPECIALTY_KEYWORDS`, `_extract_index_choice`) maps common symptom
phrases (e.g. "shoulder pain" → orthopedics) to administrative routing categories only — it
is explicitly not a diagnosis, and doctor lists are always labeled as options to choose from,
not a clinical recommendation.

## Safety boundaries
- The agent never states clinical conclusions; it only reflects what the patient said back to
  them (e.g. it books a doctor "for shoulder pain" — it does not say what the shoulder pain is).
- No action is described as successful until the corresponding capability call returns
  `success: True` (see `create_appointment`'s `sync_pending` / `reconciliation_required`
  reply text vs. `confirmed`).
- All booking, rescheduling, and cancellation requests are re-validated by
  `scheduling.py` regardless of what the AI/patient claims.

## Evaluation approach
`tests/test_core.py` exercises the full booking path, authorization, tenant isolation, and
the timeout/recovery scenario end-to-end. For AI-specific behavior, manual evaluation checklist:
1. Ambiguous request → agent asks for a specialty/concern rather than guessing.
2. Valid request → correct specialty routing, real slots only (never fabricated).
3. Selecting an option by number/ordinal word resolves correctly.
4. No key configured → fallback label shown; with a key configured → same outcomes, LLM label absent.
