"""The climate log: one compact row per sample, every zone, the weather and the sun."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "house" / "house"))
from climatelog import ClimateLog, sample  # noqa: E402


def test_sample_and_write(tmp_path):
    by = {"climate.a": {"state": "heat", "attributes": {"current_temperature": 68.5, "temperature": 68,
                                                       "hvac_action": "heating", "fan_mode": "off", "current_humidity": 40}},
          "input_select.house_climate": {"state": "Heat"},
          "weather.nws": {"attributes": {"temperature": 41, "humidity": 80, "wind_speed": 9}},
          "weather.forecast_home": {"attributes": {"cloud_coverage": 90}},
          "sun.sun": {"attributes": {"elevation": 12.3, "azimuth": 220}},
          "sensor.south_illuminance": {"state": "8200", "attributes": {"device_class": "illuminance"}},
          "sensor.south_temperature": {"state": "50.9", "attributes": {"device_class": "temperature"}},
          "sensor.gone_illuminance": {"state": "unavailable", "attributes": {"device_class": "illuminance"}}}
    row = sample(by, [{"entity": "climate.a", "name": "Main Level"}], "input_select.house_climate",
                 "weather.nws", "weather.forecast_home")
    assert row["zones"]["Main Level"] == {"now": 68.5, "set": 68.0, "mode": "heat", "act": "heating", "fan": "off", "rh": 40.0}
    assert (row["mode"], row["out"], row["cloud"], row["sun"]) == ("Heat", 41.0, 90.0, 12.3)
    assert row["lux"] == {"south_illuminance": 8200.0} and row["temps"] == {"south_temperature": 50.9}

    class H:
        climate = [1]
    lg = ClimateLog(H(), tmp_path)
    lg.write(row)
    assert lg.recent(1) == [row]
