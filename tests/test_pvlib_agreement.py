"""Pin every model against pvlib's reference implementation.

This project was written stdlib-only, with each physics model coded from its
published paper. When pvlib was adopted, the first useful thing it bought was
not better physics - it was a way to *check* the physics that was already
there. Every one of these models agreed with pvlib on the first run, to
floating-point noise or exactly.

These tests stay so that neither side can drift silently: if a future pvlib
release changes an answer, or a refactor here changes one, a test says so.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

import pandas as pd
import pvlib

from solar_twin.ac import InverterParameters, _unconstrained_ac_mw
from solar_twin.array_geometry import ashrae_iam, shaded_fraction
from solar_twin.dc import Conditions, ThermalParameters, estimate_cell_temperature, simulate_dc
from solar_twin.plant import load_plant
from solar_twin.soiling import SoilingParameters, simulate_soiling
from solar_twin.solar_position import solar_position

ROOT = Path(__file__).resolve().parents[1]


class ThermalAndDcTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(ROOT / "config/plant.json")

    def test_cell_temperature_matches_sapm(self):
        p = ThermalParameters()
        for poa, air, wind in ((1000, 25, 1), (800, 35, 2), (600, 40, 0.5), (200, 20, 5), (1100, 45, 3)):
            with self.subTest(poa=poa, air=air, wind=wind):
                self.assertAlmostEqual(
                    estimate_cell_temperature(Conditions(poa, air, wind), p),
                    float(pvlib.temperature.sapm_cell(poa, air, wind, p.a, p.b,
                                                      p.cell_module_delta_at_1000w_c)),
                    places=10)

    def test_module_power_matches_pvwatts_dc(self):
        for poa, air, wind in ((1000, 25, 1), (800, 35, 2), (200, 20, 5), (1100, 45, 3)):
            with self.subTest(poa=poa):
                result = simulate_dc(self.plant, Conditions(poa, air, wind))
                self.assertAlmostEqual(
                    result.module_available_dc_w,
                    float(pvlib.pvsystem.pvwatts_dc(
                        poa, result.cell_temperature_c, self.plant.module.power_w,
                        self.plant.module.power_temperature_coefficient_per_c)),
                    places=9)


class ArrayGeometryTests(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(ROOT / "config/plant.json")
        self.geometry = self.plant.row_geometry

    def test_row_shading_matches_shaded_fraction1d(self):
        # Across a sunset, where the shaded fraction runs 0 to nearly 1.
        for minute in (25, 30, 35, 40, 44):
            when = datetime(2025, 1, 28, 12, minute, tzinfo=timezone.utc)
            position = solar_position(self.plant.location, when)
            with self.subTest(minute=minute):
                theirs = float(pvlib.shading.shaded_fraction1d(
                    position.zenith_deg, position.azimuth_deg,
                    axis_azimuth=self.geometry.axis_azimuth_deg,
                    shaded_row_rotation=self.geometry.tilt_deg,
                    collector_width=self.geometry.collector_slant_m,
                    pitch=self.geometry.row_pitch_m))
                self.assertAlmostEqual(
                    shaded_fraction(position.zenith_deg, position.azimuth_deg, self.geometry),
                    max(0.0, min(1.0, theirs)), places=9)

    def test_incidence_angle_modifier_matches_iam_ashrae(self):
        for aoi in (0, 20, 40, 60, 75, 85, 89):
            with self.subTest(aoi=aoi):
                self.assertAlmostEqual(ashrae_iam(aoi), float(pvlib.iam.ashrae(aoi)), places=10)

    def test_sky_masking_is_small_at_this_pitch(self):
        # Recorded because it corrects a documented guess: this term was
        # omitted on the assumption it cost "a few percent". It costs 0.22%.
        self.assertAlmostEqual(self.geometry.sky_diffuse_masking_fraction, 0.0022, places=3)


class SoilingTests(unittest.TestCase):
    def test_matches_pvlib_kimber_over_three_months(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        stamps = [start + timedelta(hours=h) for h in range(24 * 90)]
        rain = [0.0] * len(stamps)
        for hour in (24 * 20 + 5, 24 * 20 + 6, 24 * 55 + 2):
            rain[hour] = 5.0
        parameters = SoilingParameters(deposition_rate_per_day=0.002)
        ours = simulate_soiling(stamps, rain, parameters)
        theirs = pvlib.soiling.kimber(
            pd.Series(rain, index=pd.DatetimeIndex(stamps)),
            cleaning_threshold=parameters.cleaning_threshold_mm,
            soiling_loss_rate=parameters.deposition_rate_per_day,
            grace_period=parameters.grace_period_days,
            max_soiling=parameters.maximum_soiling_fraction)
        worst = max(abs(o.soiling_loss_fraction - float(t)) for o, t in zip(ours.points, theirs))
        self.assertLess(worst, 1e-9)


class InverterTests(unittest.TestCase):
    def test_conversion_curve_matches_pvlib_inverter_pvwatts(self):
        p = InverterParameters()
        for dc in (0.2, 1.0, 2.5, 4.0, 5.0, 5.06):
            with self.subTest(dc=dc):
                self.assertAlmostEqual(
                    _unconstrained_ac_mw(dc, 5.0, p),
                    float(pvlib.inverter.pvwatts(dc, 5.0 / p.nominal_efficiency,
                                                 eta_inv_nom=p.nominal_efficiency)),
                    places=12)


class SolarPositionTests(unittest.TestCase):
    def test_matches_nrel_spa_reference(self):
        # NREL/TP-560-34302 A.5, under the paper's own atmosphere.
        frame = pvlib.solarposition.spa_python(
            pd.DatetimeIndex(["2003-10-17 19:30:30"], tz="UTC"),
            39.742476, -105.1786, altitude=1830.14, pressure=82000, temperature=11, delta_t=67)
        self.assertAlmostEqual(float(frame["apparent_zenith"].iloc[0]), 50.11162, places=5)
        self.assertAlmostEqual(float(frame["azimuth"].iloc[0]), 194.34024, places=5)


if __name__ == "__main__":
    unittest.main()
