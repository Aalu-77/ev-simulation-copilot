"""Tests for the simulation model, the API and the agent guardrails.

Run locally:  pip install -r requirements.txt pytest
              python -m pytest -q
"""
import pytest
from fastapi.testclient import TestClient

import simulation_tool as sim
from local_agent import ungrounded

client = TestClient(sim.app)

BASE = dict(
    vehicle_mass_kg=1500,
    battery_capacity_kwh=60,
    motor_peak_power_kw=150,
    drive_cycle="MIXED",
)


def run(**overrides):
    return sim.run_simulator(sim.SimulationRequest(**{**BASE, **overrides}))


# ---- physics sanity: do the effects point the right way? ------------------

def test_heavier_car_uses_more_energy():
    assert run(vehicle_mass_kg=2200).energy_kwh_per_100km > run().energy_kwh_per_100km


def test_regen_saves_energy():
    on = run(regen_enabled=True).energy_kwh_per_100km
    off = run(regen_enabled=False).energy_kwh_per_100km
    assert on < off


def test_cold_weather_reduces_range():
    assert run(ambient_temp_c=-10).estimated_range_km < run(ambient_temp_c=20).estimated_range_km


def test_drag_matters_on_highway():
    high = run(drive_cycle="HIGHWAY", drag_area_m2=1.0).energy_kwh_per_100km
    low = run(drive_cycle="HIGHWAY", drag_area_m2=0.5).energy_kwh_per_100km
    assert high > low


def test_weak_motor_triggers_warning():
    result = run(motor_peak_power_kw=35, drive_cycle="HIGHWAY")
    assert any("power limit" in w for w in result.warnings)


def test_regression_known_result():
    """Pins the current model output so accidental physics changes are caught.
    If you change the model on purpose, update this number."""
    r = run(vehicle_mass_kg=1000, battery_capacity_kwh=10,
            motor_peak_power_kw=30, drive_cycle="URBAN")
    assert r.energy_kwh_per_100km == pytest.approx(8.37)
    assert r.distance_km == pytest.approx(1.32)


# ---- API behaviour ---------------------------------------------------------

def test_create_and_fetch_run():
    created = client.post("/simulations", json=BASE)
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    fetched = client.get(f"/simulations/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == run_id


@pytest.mark.parametrize("bad", [
    {"battery_capacity_kwh": 600},   # out of range
    {"turbo": True},                 # unknown parameter
    {"drive_cycle": "WLTC"},         # unknown drive cycle
])
def test_invalid_requests_are_rejected(bad):
    assert client.post("/simulations", json={**BASE, **bad}).status_code == 422


def test_unknown_run_id_is_404():
    assert client.get("/simulations/does-not-exist").status_code == 404


# ---- agent guardrail: values must come from the user ----------------------

def user(*texts):
    return [{"role": "user", "content": t} for t in texts]


def test_invented_values_are_flagged():
    args = dict(vehicle_mass_kg=1200, battery_capacity_kwh=40, motor_peak_power_kw=100)
    assert len(ungrounded(args, user("I need to run a simulation"))) == 3


def test_stated_values_with_unit_conversion_pass():
    args = dict(vehicle_mass_kg=1200, battery_capacity_kwh=40, motor_peak_power_kw=100)
    assert ungrounded(args, user("1.2 tonnes, 40 kWh, 100 kW")) == []
