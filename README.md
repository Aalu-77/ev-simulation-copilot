# EV Simulation Copilot

![CI](https://github.com/Aalu-77/ev-simulation-copilot/actions/workflows/ci.yml/badge.svg)

An AI agent that turns plain-language requests into electric vehicle
simulation runs and explains the results.

> "How far can a 1.2-tonne city EV with a 40 kWh battery and a 100 kW motor
> go in stop-and-go traffic at -5 °C?"

The language model **never calculates anything itself**. It translates the
request into validated parameters, calls a deterministic physics model as a
tool, and explains what comes back.

**Status:** work in progress. A local-LLM prototype works end to end. Azure
deployment, retrieval and evaluations are planned (see Roadmap).

## How it works

```
User message
    |
    v
Router (is a tool needed at all?)
    |
    v
Agent (LLM + system prompt + tool definitions)
    |  emits a tool call with JSON arguments
    v
Guardrails (missing values? values the user never stated?)
    |
    v
FastAPI + Pydantic validation  ->  physics model  ->  results
    |
    v
Agent explains the results to the user
```

| File | Purpose |
|---|---|
| `simulation_tool.py` | Physics model, Pydantic schemas, FastAPI service, tool definitions |
| `local_agent.py` | Chat loop for a local LLM (Ollama) with routing and guardrails |
| `agent_system_prompt.md` | The agent's system prompt |
| `tests/test_simulation.py` | Tests for the physics, the API and the guardrails |
| `.github/workflows/ci.yml` | Runs the tests on every push |

## The simulation model

A point-mass vehicle on a flat road is driven through a speed-vs-time curve in
1-second steps. For each step:

```
F = m_eff * a  +  Crr * m * g  +  0.5 * rho * CdA * v^2
P_wheel = F * v
P_battery = P_wheel / eta_drive            (driving)
P_battery = P_wheel * eta_regen            (braking, if regen is on)
```

Auxiliary load (electronics, heating or cooling) is added on top, and usable
battery capacity is reduced in the cold. The result reports distance, energy
consumption (kWh/100 km), estimated range, final state of charge, peak battery
power and warnings (for example when the motor power limit is reached).

**Limits:** flat road, constant efficiencies, simplified regenerative braking
and temperature effects, and synthetic drive cycles (URBAN, HIGHWAY, MIXED),
not the official WLTC or NEDC. **Results are not validated against real vehicle
data.**

## Guardrails for small models

Small local models (tested with `llama3.2:3b`) tend to call a tool on every
message and to copy example values from the prompt. These layers are enforced
in code, not only in the prompt:

- **Router:** a cheap classification call decides whether the turn needs tools
  at all, so greetings and general questions never trigger a simulation.
- **Tool gating:** `get_simulation` is only offered once a run ID exists in the
  conversation.
- **Argument validation:** calls with missing required parameters are never
  sent to the API.
- **Grounding check:** required numbers must appear in the user's own messages
  (tonnes, kg, Wh and kWh conversions are accepted). Invented values are
  rejected and the agent asks the user instead.
- **Strict schemas:** Pydantic enforces ranges and rejects unknown parameters
  (HTTP 422).

## Run it

Requires Python 3.12 and [Ollama](https://ollama.com).

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows (use source .venv/bin/activate on macOS/Linux)
python -m pip install -r requirements.txt

ollama pull llama3.2:3b

# Terminal A: the simulator API (interactive docs at http://127.0.0.1:8000/docs)
python -m uvicorn simulation_tool:app --reload

# Terminal B: the chat agent
python local_agent.py
```

Set `LOCAL_MODEL` to try another Ollama model, for example
`LOCAL_MODEL=qwen3:4b`.

Run the tests:

```bash
python -m pip install pytest
python -m pytest -q
```

## Example

```
you> vehicle mass 1000, battery 100, motor power 50 and drive cycle is URBAN
  [router] TOOL
  [tool] run_simulation({'vehicle_mass_kg': 1000, 'battery_capacity_kwh': 100,
                         'motor_peak_power_kw': 50, 'drive_cycle': 'URBAN'})

agent> Distance 1.32 km, 8.37 kWh/100 km, estimated range about 1194 km,
       final state of charge 99.9%, peak battery power 17.4 kW.
```

## Known issues

- The agent sometimes invents causes when explaining results (for example
  claiming the motor "limited" a run when no warning says so). Planned fix:
  return an energy breakdown (drag, rolling, regen, auxiliary) from the tool so
  explanations come from data.
- The router can misclassify short follow-ups such as "yes".
- Small local models are much less reliable than hosted ones at tool use.

## Roadmap

- [x] Physics model, API and tests
- [x] Local agent loop with guardrails
- [x] CI with GitHub Actions
- [ ] Energy breakdown in the simulation result
- [ ] Microsoft Foundry deployment (agent service)
- [ ] Retrieval over technical documents (Azure AI Search) with citations
- [ ] Evaluation set measuring parameter extraction and answer grounding
- [ ] Deployment on Azure Container Apps via GitHub Actions
