"""simulation backend + tool contract for the Simulation Copilot.

Contains a simple physics-based EV longitudinal-dynamics model (no external
simulator needed), strict Pydantic schemas, and a FastAPI service.

Run:  pip install fastapi uvicorn pydantic
      uvicorn simulation_tool:app --reload
Docs: http://127.0.0.1:8000/docs

MODEL SCOPE:
- Flat road, point-mass vehicle, constant efficiencies, simple HVAC/battery
  temperature effects, synthetic drive cycles (NOT the official WLTC/NEDC).
- Not validated against real vehicle data.
"""
from enum import Enum
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

class DriveCycle(str, Enum):
    URBAN = "URBAN"      # stop-and-go, max 50 km/h
    HIGHWAY = "HIGHWAY"  # sustained 100-120 km/h
    MIXED = "MIXED"      # urban followed by highway


class SimulationRequest(BaseModel):
    """Parameters for one EV drivetrain simulation run."""

    # Reject any parameter the agent invents instead of silently ignoring it.
    model_config = ConfigDict(extra="forbid")

    vehicle_mass_kg: float = Field(
        ..., ge=800, le=3500, description="Curb mass without payload, in kg."
    )
    payload_kg: float = Field(0, ge=0, le=1000, description="Passengers and cargo, in kg.")
    battery_capacity_kwh: float = Field(
        ..., ge=10, le=200, description="Usable battery capacity at 20 C, in kWh."
    )
    motor_peak_power_kw: float = Field(..., ge=30, le=500, description="Peak motor power, in kW.")
    drive_cycle: DriveCycle = Field(..., description="Synthetic drive cycle to simulate.")
    ambient_temp_c: float = Field(
        20, ge=-30, le=50, description="Ambient temperature, in degrees Celsius."
    )
    regen_enabled: bool = Field(True, description="Whether regenerative braking is on.")
    drag_area_m2: float = Field(
        0.65, ge=0.4, le=1.2, description="Drag coefficient times frontal area (CdA), in m^2."
    )
    rolling_resistance_coeff: float = Field(
        0.010, ge=0.006, le=0.020, description="Tyre rolling resistance coefficient (-)."
    )

    @model_validator(mode="after")
    def check_total_mass(self):
        if self.vehicle_mass_kg + self.payload_kg > 4000:
            raise ValueError("vehicle_mass_kg + payload_kg must not exceed 4000 kg")
        return self


class SimulationResult(BaseModel):
    distance_km: float
    energy_kwh_per_100km: float
    estimated_range_km: float
    final_soc_percent: float  # state of charge after one cycle, starting at 100 %
    peak_battery_power_kw: float
    warnings: list[str] = []


class SimulationRun(BaseModel):
    run_id: str
    request: SimulationRequest
    result: SimulationResult


#physics

G = 9.81          # m/s^2
RHO_AIR = 1.2     # kg/m^3
ETA_DRIVE = 0.90  # battery -> wheels (inverter + motor + gearbox)
ETA_REGEN = 1.5  # wheels -> battery (lower than drive: friction brakes, losses)
ROT_INERTIA = 1.05  # equivalent-mass factor for rotating parts
DT = 1.0          # s

# (duration_s, target_speed_kmh): linear ramp from the previous speed.
_URBAN = [(20, 0), (15, 50), (25, 50), (12, 0), (10, 0), (12, 35), (20, 35),
          (10, 0), (10, 0), (15, 50), (20, 50), (15, 0)]
_HIGHWAY = [(40, 100), (300, 100), (20, 120), (200, 120), (30, 100), (150, 100), (40, 0)]
CYCLES = {
    DriveCycle.URBAN: _URBAN,
    DriveCycle.HIGHWAY: _HIGHWAY,
    DriveCycle.MIXED: _URBAN + _HIGHWAY,
}


def speed_profile(cycle: DriveCycle) -> list[float]:
    """Speed in m/s at each DT step."""
    v, out = 0.0, []
    for duration, target_kmh in CYCLES[cycle]:
        target = target_kmh / 3.6
        steps = int(duration / DT)
        for i in range(1, steps + 1):
            out.append(v + (target - v) * i / steps)
        v = target
    return out


def hvac_power_kw(temp_c: float) -> float:
    base = 0.4  # electronics
    if temp_c < 10:
        return base + (10 - temp_c) * 0.10   # cabin heating
    if temp_c > 25:
        return base + (temp_c - 25) * 0.08   # cooling
    return base


def run_simulator(req: SimulationRequest) -> SimulationResult:
    mass = req.vehicle_mass_kg + req.payload_kg
    eff_mass = mass * ROT_INERTIA
    profile = speed_profile(req.drive_cycle)
    motor_limit_w = req.motor_peak_power_kw * 1000
    aux_w = hvac_power_kw(req.ambient_temp_c) * 1000

    # Cold battery: less usable capacity (simple linear derating below 15 C).
    usable_kwh = req.battery_capacity_kwh * (1 - 0.006 * max(0.0, 15 - req.ambient_temp_c))

    energy_wh = distance_m = peak_w = 0.0
    limited_s = 0
    v_prev = 0.0
    for v in profile:
        a = (v - v_prev) / DT
        v_avg = (v + v_prev) / 2
        f = (eff_mass * a
             + req.rolling_resistance_coeff * mass * G * (1 if v_avg > 0 else 0)
             + 0.5 * RHO_AIR * req.drag_area_m2 * v_avg ** 2)
        p_wheel = f * v_avg
        if p_wheel > motor_limit_w * ETA_DRIVE:
            p_wheel = motor_limit_w * ETA_DRIVE
            limited_s += 1
        if p_wheel >= 0:
            p_batt = p_wheel / ETA_DRIVE
        else:
            p_batt = p_wheel * ETA_REGEN if req.regen_enabled else 0.0
        p_batt += aux_w
        energy_wh += p_batt * DT / 3600
        peak_w = max(peak_w, p_batt)
        distance_m += v_avg * DT
        v_prev = v

    distance_km = distance_m / 1000
    kwh_per_100km = energy_wh / 1000 / distance_km * 100
    warnings = []
    if limited_s:
        warnings.append(
            f"Motor power limit reached for {limited_s} s; the cycle was not fully followed."
        )
    if req.ambient_temp_c < 0:
        warnings.append("Below 0 C: real cold-weather losses are likely higher than modelled.")
    return SimulationResult(
        distance_km=round(distance_km, 2),
        energy_kwh_per_100km=round(kwh_per_100km, 2),
        estimated_range_km=round(usable_kwh / kwh_per_100km * 100, 1),
        final_soc_percent=round(100 - energy_wh / 1000 / usable_kwh * 100, 1),
        peak_battery_power_kw=round(peak_w / 1000, 1),
        warnings=warnings,
    )


#  API

app = FastAPI(title="Simulation Tool")
RUNS: dict[str, SimulationRun] = {}  # in-memory for week 1; swap for storage later


@app.post("/simulations", response_model=SimulationRun, status_code=201)
def create_simulation(req: SimulationRequest) -> SimulationRun:
    run = SimulationRun(run_id=str(uuid4()), request=req, result=run_simulator(req))
    RUNS[run.run_id] = run
    return run


@app.get("/simulations/{run_id}", response_model=SimulationRun)
def get_simulation(run_id: str) -> SimulationRun:
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return RUNS[run_id]


def tool_definitions() -> list[dict]:
    """JSON-schema tool definitions to hand to the agent.
    Adapt the wrapper to the function-tool format of the Foundry SDK you use."""
    return [
        {
            "name": "run_simulation",
            "description": (
                "Run one EV drivetrain simulation. Only call when every required "
                "parameter is known; never guess values."
            ),
            "parameters": SimulationRequest.model_json_schema(),
        },
        {
            "name": "get_simulation",
            "description": "Fetch a previous simulation run. Requires a run_id (a UUID) that the "
    "user gave you or that appeared in an earlier tool result. "
    "Never call it without a run_id.",
            "parameters": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
                "additionalProperties": False,
            },
        },
    ]


if __name__ == "__main__":
    import json

    print(json.dumps(tool_definitions(), indent=2))
