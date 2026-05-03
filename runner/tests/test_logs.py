import asyncio
import json
import pytest
from runner.logs import read_last_n, stream_log, get_base_dir


@pytest.fixture(autouse=True)
def setup_logs_dir(tmp_env):
    (tmp_env / "logs").mkdir(exist_ok=True)


def _write_log(tmp_env, name: str, lines: list[str]):
    log_path = tmp_env / "logs" / f"{name}.jsonl"
    log_path.write_text("\n".join(lines) + "\n")


def test_read_last_n_returns_lines(tmp_env):
    _write_log(tmp_env, "myapp", [json.dumps({"type": "assistant", "n": i}) for i in range(20)])
    lines = read_last_n("myapp", n=5)
    assert len(lines) == 5
    assert json.loads(lines[-1])["n"] == 19


def test_read_last_n_missing_file_returns_empty(tmp_env):
    lines = read_last_n("noexist", n=10)
    assert lines == []


def test_read_last_n_fewer_than_n_lines(tmp_env):
    _write_log(tmp_env, "small", ["line1", "line2"])
    lines = read_last_n("small", n=100)
    assert lines == ["line1", "line2"]


async def test_stream_log_yields_existing_lines(tmp_env):
    _write_log(tmp_env, "stream-test", ["line1", "line2", "line3"])
    received = []

    async def collect():
        async for line in stream_log("stream-test"):
            received.append(line)
            if len(received) == 3:
                break

    await asyncio.wait_for(collect(), timeout=2.0)
    assert received == ["line1", "line2", "line3"]


async def test_stream_log_picks_up_new_lines(tmp_env):
    log_path = tmp_env / "logs" / "live.jsonl"
    log_path.write_text("")
    received = []

    async def collect():
        async for line in stream_log("live"):
            received.append(line)
            if len(received) == 2:
                break

    async def writer():
        await asyncio.sleep(0.1)
        with log_path.open("a") as f:
            f.write("first\n")
        await asyncio.sleep(0.1)
        with log_path.open("a") as f:
            f.write("second\n")

    await asyncio.gather(
        asyncio.wait_for(collect(), timeout=3.0),
        writer(),
    )
    assert received == ["first", "second"]
