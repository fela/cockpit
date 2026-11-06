from cockpit import machine

MEMINFO = """MemTotal:       33554432 kB
MemFree:         2097152 kB
MemAvailable:   25165824 kB
SwapTotal:      16777216 kB
SwapFree:        12582912 kB
"""
GB = 1024**3


def test_an_overloaded_processor_turns_the_lamp_dark_yellow_at_most() -> None:
    reading = machine.measure("24.0 16.0 8.0 2/100 1000", MEMINFO, 8, 1000 * GB, 400 * GB)
    assert reading["cpu"] == "busy"
    assert reading["memory"] == "under"
    assert reading["disk"] == "under"
    assert reading["level"] == "busy"
    assert reading["load"] == 24.0
    assert reading["memory_total_gb"] == 32
    assert reading["memory_used_gb"] == 8.0
    assert reading["swap_used_gb"] == 4.0
    assert reading["disk_free_gb"] == 400


def test_the_lamp_shows_the_worst_reading() -> None:
    reading = machine.measure("40.0 4.0 4.0 1/400 1", MEMINFO, 8, 1000 * GB, 100 * GB)
    assert (reading["cpu"], reading["disk"], reading["level"]) == ("busy", "near", "near")


def test_the_processor_turns_dark_yellow_from_two_jobs_per_core() -> None:
    assert machine.measure("15.9 0 0 1/1 1", MEMINFO, 8, GB, GB)["cpu"] == "under"
    assert machine.measure("16.0 0 0 1/1 1", MEMINFO, 8, GB, GB)["cpu"] == "busy"


def test_memory_and_disk_turn_orange_then_red() -> None:
    assert machine.level(0.79, 0.8, 0.9) == "under"
    assert machine.level(0.8, 0.8, 0.9) == "near"
    assert machine.level(0.9, 0.8, 0.9) == "over"


def test_the_host_reads() -> None:
    reading = machine.read()
    # A system without /proc (macOS) says so instead of reading.
    assert reading["level"] in machine.LEVELS if "unavailable" not in reading else reading["unavailable"]


def test_system_without_proc_has_no_error_or_load_rack(monkeypatch, caplog):
    from pathlib import Path

    exists, read_text = Path.exists, Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/loadavg":
            raise FileNotFoundError("/proc/loadavg is absent")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "exists", lambda path: False if str(path) == "/proc/loadavg" else exists(path))
    assert machine.read() == {"unavailable": "Load readings are not available on this system"}
    assert not caplog.records
