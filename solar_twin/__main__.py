import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .ac import InverterParameters, simulate_ac
from .dc import Conditions, simulate_dc
from .dc_electrical import evaluate_dc_electrical
from .grid import GridLossParameters, simulate_grid_export
from .plant import load_plant
from .timeseries import simulate_weather_series, snapshot_to_dict
from .weather import fetch_hourly_weather


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate or simulate the virtual solar plant")
    parser.add_argument("--config", default="config/plant.json")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--topology", action="store_true", help="Print the asset topology")
    action.add_argument("--simulate", metavar="JSON", help="Evaluate a JSON list of named DC scenarios")
    action.add_argument("--simulate-ac", metavar="JSON", help="Evaluate named inverter-terminal AC scenarios")
    action.add_argument("--simulate-electrical", metavar="JSON",
                         help="Evaluate named DC string voltage/current feasibility scenarios")
    action.add_argument("--simulate-grid", metavar="JSON",
                         help="Evaluate named net-export-at-POI scenarios (transformer/cable losses, curtailment)")
    action.add_argument("--simulate-weather", metavar=("START", "END"), nargs=2,
                         help="Fetch real hourly weather for START..END (YYYY-MM-DD, inclusive UTC) and simulate each hour")
    parser.add_argument("--ac", action="store_true",
                         help="With --simulate-weather, run inverter-terminal AC instead of DC")
    parser.add_argument("--inverter-model", metavar="JSON", help="Override AC model parameters from JSON")
    parser.add_argument("--grid-loss-model", metavar="JSON",
                         help="Override grid loss model parameters from JSON (with --simulate-grid)")
    args = parser.parse_args()
    if args.ac and not args.simulate_weather:
        parser.error("--ac requires --simulate-weather")
    if args.inverter_model and not (args.simulate_ac or args.simulate_grid or (args.simulate_weather and args.ac)):
        parser.error("--inverter-model requires --simulate-ac, --simulate-grid, or --simulate-weather --ac")
    if args.grid_loss_model and not args.simulate_grid:
        parser.error("--grid-loss-model requires --simulate-grid")
    try:
        plant = load_plant(args.config)
        if args.simulate or args.simulate_ac or args.simulate_electrical or args.simulate_grid:
            parameters = InverterParameters()
            if args.inverter_model:
                parameters = InverterParameters(**json.loads(Path(args.inverter_model).read_text(encoding="utf-8")))
            grid_loss_parameters = GridLossParameters()
            if args.grid_loss_model:
                grid_loss_parameters = GridLossParameters(
                    **json.loads(Path(args.grid_loss_model).read_text(encoding="utf-8")))
            path = args.simulate or args.simulate_ac or args.simulate_electrical or args.simulate_grid
            scenarios = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(scenarios, list) or not scenarios:
                raise ValueError("Scenarios must be a nonempty JSON list")
            results = []
            for scenario in scenarios:
                required = {"name", "conditions"}
                allowed = required | ({"inverter_air_temperature_c"} if args.simulate_ac or args.simulate_grid else set())
                if not isinstance(scenario, dict) or not required <= scenario.keys() or not scenario.keys() <= allowed:
                    raise ValueError("Each scenario requires name and conditions, with optional inverter_air_temperature_c for AC/grid")
                if not isinstance(scenario["name"], str) or not scenario["name"].strip():
                    raise ValueError("Scenario name must be a nonempty string")
                conditions = Conditions(**scenario["conditions"])
                if args.simulate_ac:
                    result = simulate_ac(plant, conditions, parameters,
                                          inverter_air_temperature_c=scenario.get("inverter_air_temperature_c"))
                elif args.simulate_grid:
                    result = simulate_grid_export(plant, conditions, parameters, grid_loss_parameters=grid_loss_parameters,
                                                   inverter_air_temperature_c=scenario.get("inverter_air_temperature_c"))
                elif args.simulate_electrical:
                    result = evaluate_dc_electrical(plant, conditions)
                else:
                    result = simulate_dc(plant, conditions)
                results.append({"name": scenario["name"], **asdict(result)})
            model = "Sandia temperature + PVWatts v5 DC equation"
            if args.simulate_ac:
                model += " + conditional inverter AC"
            elif args.simulate_grid:
                model += " + conditional inverter AC + grid-side losses and net export"
            elif args.simulate_electrical:
                model += " + string voltage/current feasibility"
            output = {"model": model, "plant": plant.summary(), "scenarios": results}
        elif args.simulate_weather:
            parameters = InverterParameters()
            if args.inverter_model:
                parameters = InverterParameters(**json.loads(Path(args.inverter_model).read_text(encoding="utf-8")))
            start, end = args.simulate_weather
            observations = fetch_hourly_weather(plant.location, start, end)
            snapshots = simulate_weather_series(plant, observations, ac=args.ac, inverter_parameters=parameters)
            output = {"model": ("PSA solar position + isotropic transposition + Sandia temperature + PVWatts DC"
                                + (" + conditional inverter AC" if args.ac else "")),
                      "plant": plant.summary(),
                      "scenarios": [snapshot_to_dict(s) for s in snapshots]}
        else:
            output = plant.topology() if args.topology else plant.summary()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"Invalid input: {exc}\n")
    print(json.dumps(output, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
