"""test the Simulation Copilot idea with a LOCAL LLM.

Setup
  1. Install Ollama, then pull a model that supports tool calling:
         ollama pull llama3.2:3b
  2. Terminal A (the simulator API):
         uvicorn simulation_tool:app --reload
  3. Terminal B (this chat loop):
         pip install openai httpx
         python local_agent.py
"""
import json
import os
from pathlib import Path
import re

import httpx
from openai import OpenAI

from simulation_tool import tool_definitions, SimulationRequest

SIM_API = os.getenv("SIM_API", "http://127.0.0.1:8000")
MODEL = os.getenv("LOCAL_MODEL", "llama3.2:3b")
# Ollama's OpenAI-compatible endpoint. The key is required by the client
# library but ignored by Ollama.
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
SYSTEM_PROMPT = (Path(__file__).parent / "agent_system_prompt.md").read_text()
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
REQUIRED = SimulationRequest.model_json_schema().get("required", [])

NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
GROUNDING = {  # accepted ways a stated number can become the tool value
    "vehicle_mass_kg": lambda n: (n, n * 1000),       # kg or tonnes
    "battery_capacity_kwh": lambda n: (n, n / 1000),  # kWh or Wh
    "motor_peak_power_kw": lambda n: (n,),
}

def ungrounded(args: dict, messages: list[dict]) -> list[str]:
    """Required numbers that the user never actually stated."""
    nums = set()
    for m in messages:
        if m["role"] == "user":
            for s in NUM_RE.findall(m.get("content") or ""):
                nums.add(float(s.replace(",", ".")))
    return [
        k for k, conv in GROUNDING.items()
        if k in args and not any(
            abs(args[k] - c) <= 1e-6 * max(1, abs(args[k]))
            for n in nums for c in conv(n)
        )
    ]

def inline_refs(schema: dict) -> dict:
    """Pydantic puts enums under $defs and points to them with $ref. Small
    local models often handle that badly, so inline everything."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = walk(defs[node["$ref"].split("/")[-1]])
                return {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


#definition in the OpenAI-style {"type": "function", ...} envelope.
TOOLS = [
    {"type": "function", "function": {**t, "parameters": inline_refs(t["parameters"])}}
    for t in tool_definitions()
]

ROUTER_PROMPT = """Classify the user's LAST message. Reply with exactly one word.

TOOL = the user wants a simulation calculation (range, consumption, estimate,
comparison, what-if) for a vehicle, OR is giving vehicle details or values for
one, OR asks about an earlier simulation run.
CHAT = anything else: greetings, thanks, general questions about EVs or
physics, questions about what you can do.

Examples:
hi -> CHAT
What is regenerative braking? -> CHAT
What can you do? -> CHAT
How far can a 1.2 t EV with a 40 kWh battery go in the city? -> TOOL
1200 kg, 40 kWh, 100 kW, urban -> TOOL
What if the battery were 60 kWh? -> TOOL"""


def needs_tools(messages: list[dict]) -> bool:
    recent = [
        m for m in messages
        if m["role"] in ("user", "assistant") and m.get("content")
    ][-4:]
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in recent)
    out = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        max_tokens=5,
        messages=[
            {"role": "system", "content": ROUTER_PROMPT},
            {"role": "user", "content": convo},
        ],
    ).choices[0].message.content or ""
    return "TOOL" in out.upper()


def tools_for(messages: list[dict]) -> list[dict]:
    """Only offer get_simulation once a run_id has actually appeared."""
    text = " ".join(str(m.get("content") or "") for m in messages)
    has_run = bool(UUID_RE.search(text))
    return [
        t for t in TOOLS
        if t["function"]["name"] != "get_simulation" or has_run
    ]

def execute_tool(name: str, args: dict, messages: list[dict]) -> str:
    """Run the tool the model asked for and return the result as text.
    Errors (e.g. a 422 for an out-of-range value) are returned to the model
    so it can correct itself or ask the user."""

    if name == "run_simulation":
        missing = [k for k in REQUIRED if k not in args]
        if missing:
            return json.dumps({
                "error": "missing required parameters",
                "missing": missing,
                "instruction": "Do not run the simulation. Ask the user for these values.",
            })
        
        bad = ungrounded(args, messages)                     
        if bad:                                              
            return json.dumps({                              
                "error": "values not stated by the user",   
                "parameters": bad,                          
                "instruction": "Do not run the simulation. Ask the user for these values.",  # <-- NEW
            })  
    if name == "get_simulation" and not UUID_RE.fullmatch(str(args.get("run_id", ""))):
        return json.dumps({
            "error": "no valid run_id",
            "instruction": "Ask the user for a run_id. Do not call this tool without one.",
        })
    
    try:
        if name == "run_simulation":
            r = httpx.post(f"{SIM_API}/simulations", json=args, timeout=30)
        elif name == "get_simulation":
            r = httpx.get(f"{SIM_API}/simulations/{args['run_id']}", timeout=30)
        else:
            return json.dumps({"error": f"unknown tool: {name}"})
    except (httpx.HTTPError, KeyError) as exc:
        return json.dumps({"error": f"tool call failed: {exc}"})
    if r.status_code >= 400:
        return json.dumps({"error": r.status_code, "detail": r.text})
    return r.text


def ask(messages: list[dict], max_rounds: int = 6) -> str:
    """One user turn: call the model, run any tools it requests, repeat until
    it answers in plain text."""

    use_tools = needs_tools(messages)
    print(f"  [router] {'TOOL' if use_tools else 'CHAT'}")

    for _ in range(max_rounds):
        kwargs = {"tools": tools_for(messages)} if use_tools else {}
        reply = client.chat.completions.create(
            model=MODEL, messages=messages, temperature=0, **kwargs
        ).choices[0].message


        if not reply.tool_calls:  # plain answer: done
            messages.append({"role": "assistant", "content": reply.content or ""})
            return reply.content or ""

        messages.append(reply.model_dump(exclude_none=True))  # keep the tool request
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                result = json.dumps({"error": "arguments were not valid JSON"})
            else:
                print(f"  [tool] {call.function.name}({args})")
                result = execute_tool(call.function.name, args, messages)
                if result.startswith('{"error"'):
                    print(f"  [blocked] {result[:110]}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "Stopped: too many tool calls in one turn."


def main() -> None:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    print(f"Simulation Copilot (local model: {MODEL}). Ctrl+C to quit.")
    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        messages.append({"role": "user", "content": user})
        print("\nagent>", ask(messages))


if __name__ == "__main__":
    main()
