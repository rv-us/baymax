from streaming.audio_receiver import AudioReceiver, build_command, dbfs

CHUNK = b"\x01\x00" * 320  # 20 ms of 16 kHz mono


def filled(n, start=100.0, **kw):
    rx = AudioReceiver(**kw)
    for i in range(n):
        rx._append(start + 0.02 * (i + 1), CHUNK)
    return rx


def test_command_uses_port_and_rate():
    cmd = build_command(port=6000, latency_ms=40, rate=16000)
    assert "port=6000" in cmd and "latency=40" in cmd
    assert any("rate=16000" in a for a in cmd)
    assert cmd[-2:] == ["fd=1", "sync=false"]


def test_since_returns_audio_after_t_and_its_start_time():
    rx = filled(10)  # chunks end at 100.02 ... 100.20
    pcm, t0 = rx.since(100.10)
    assert len(pcm) == 5 * len(CHUNK)
    assert abs(t0 - 100.10) < 1e-9


def test_chunks_since_keeps_each_arrival_time():
    rx = filled(5)  # chunks end at 100.02 ... 100.10
    got = rx.chunks_since(100.06)
    assert [round(t, 2) for t, _ in got] == [100.08, 100.10]
    assert rx.chunks_since(got[-1][0]) == []


def test_since_with_nothing_newer():
    assert filled(3).since(200.0) == (b"", None)


def test_latest_trims_to_requested_length():
    rx = filled(10)
    assert len(rx.latest(0.05)) == 800 * 2  # 50 ms
    assert len(rx.latest(5.0)) == 10 * len(CHUNK)


def test_old_audio_is_dropped_after_keep_s():
    rx = filled(100, keep_s=1.0)  # 2 s of audio
    assert len(rx.latest(10.0)) == 50 * len(CHUNK)
    assert rx.chunk_count == 100


def test_dbfs():
    assert dbfs(b"") == -120.0
    assert dbfs(b"\x00\x00" * 10) == -120.0
    assert dbfs(b"\xff\x7f" * 10) > -0.1
