import pytest
from zonefile import (
    today_serial,
    next_serial,
    parse_serial,
    create_zone,
    append_to_zone,
)

SAMPLE_ZONE = """\
$ORIGIN example.com.
$TTL 300
@   IN SOA ns.example.com. hostmaster.example.com. (
        2026050401   ; serial
        3600         ; refresh
        600          ; retry
        604800       ; expire
        300 )        ; minimum

    IN NS  ns.example.com.
ns  IN A   192.168.3.70

; --- Hosts
host1        IN A  192.168.3.1  ; aa:bb:cc:dd:ee:ff

; --- Wildcard -> Traefik
*   IN A  192.168.3.70
    IN A  192.168.3.71
@   IN A  192.168.3.70
"""


class TestSerial:
    def test_today_serial_is_10_digits(self):
        s = today_serial()
        assert 2026010101 <= s <= 2099123199

    def test_next_serial_increments_by_one(self):
        assert next_serial(2026050401) == 2026050402

    def test_parse_serial_from_soa(self):
        assert parse_serial(SAMPLE_ZONE) == 2026050401

    def test_parse_serial_raises_on_missing(self):
        with pytest.raises(ValueError, match="serial"):
            parse_serial("$ORIGIN example.com.\n")


class TestCreateZone:
    def test_contains_origin(self):
        z = create_zone("example.com", 2026050401, "192.168.3.70", [], ["192.168.3.70"])
        assert "$ORIGIN example.com." in z

    def test_contains_serial(self):
        z = create_zone("example.com", 2026050401, "192.168.3.70", [], ["192.168.3.70"])
        assert "2026050401" in z

    def test_contains_host_a_record(self):
        z = create_zone(
            "example.com",
            2026050401,
            "192.168.3.70",
            [("myhost", "192.168.3.5", "aa:bb:cc:dd:ee:ff")],
            ["192.168.3.70"],
        )
        assert "myhost" in z
        assert "192.168.3.5" in z
        assert "aa:bb:cc:dd:ee:ff" in z

    def test_host_without_mac_has_no_comment(self):
        z = create_zone(
            "example.com", 2026050401, "192.168.3.70",
            [("bare", "192.168.3.9", None)],
            ["192.168.3.70"],
        )
        assert "bare" in z
        bare_line = [l for l in z.splitlines() if "bare" in l][0]
        assert ";" not in bare_line

    def test_wildcard_block_present(self):
        z = create_zone(
            "example.com", 2026050401, "192.168.3.70", [],
            ["192.168.3.70", "192.168.3.71"],
        )
        assert "*   IN A  192.168.3.70" in z
        assert "    IN A  192.168.3.71" in z

    def test_at_record_points_to_first_wildcard_ip(self):
        z = create_zone(
            "example.com", 2026050401, "192.168.3.70", [], ["192.168.3.70"]
        )
        assert "@   IN A  192.168.3.70" in z


class TestAppendToZone:
    def test_increments_serial(self):
        new_text, old, new = append_to_zone(SAMPLE_ZONE, [])
        assert old == 2026050401
        assert new == 2026050402
        assert "2026050402" in new_text

    def test_new_host_inserted_before_wildcard(self):
        new_text, _, _ = append_to_zone(
            SAMPLE_ZONE, [("newhost", "192.168.3.99", None)]
        )
        wildcard_pos = new_text.index("; --- Wildcard")
        host_pos = new_text.index("newhost")
        assert host_pos < wildcard_pos

    def test_existing_hosts_preserved(self):
        new_text, _, _ = append_to_zone(
            SAMPLE_ZONE, [("newhost", "192.168.3.99", None)]
        )
        assert "host1" in new_text

    def test_mac_written_as_comment(self):
        new_text, _, _ = append_to_zone(
            SAMPLE_ZONE, [("h2", "192.168.3.5", "BB:CC:DD:EE:FF:00")]
        )
        assert "; BB:CC:DD:EE:FF:00" in new_text

    def test_multiple_new_hosts(self):
        new_text, _, _ = append_to_zone(
            SAMPLE_ZONE,
            [("alpha", "192.168.3.10", None), ("beta", "192.168.3.11", None)],
        )
        assert "alpha" in new_text
        assert "beta" in new_text
