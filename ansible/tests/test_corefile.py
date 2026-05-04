import pytest
from corefile import remove_stanza, update_corefile

SAMPLE_COREFILE = """\
home.:53 {
  file /etc/coredns/zones/home.db
  log
  errors
}

schollar.dev:53 {
  file /etc/coredns/zones/schollar.dev.db
  log
  errors
}

.:53 {
  cache 30
  forward . 1.1.1.1 9.9.9.9
  log
  errors
}
"""


class TestRemoveStanza:
    def test_removes_target_stanza(self):
        new_text, found = remove_stanza(SAMPLE_COREFILE, "home.")
        assert found is True
        assert "home.:53" not in new_text

    def test_preserves_other_stanzas(self):
        new_text, _ = remove_stanza(SAMPLE_COREFILE, "home.")
        assert "schollar.dev:53" in new_text
        assert ".:53" in new_text

    def test_returns_false_when_not_found(self):
        _, found = remove_stanza(SAMPLE_COREFILE, "nonexistent.")
        assert found is False

    def test_idempotent(self):
        text, _ = remove_stanza(SAMPLE_COREFILE, "home.")
        text2, found = remove_stanza(text, "home.")
        assert found is False
        assert "schollar.dev:53" in text2

    def test_result_has_no_double_blank_lines(self):
        new_text, _ = remove_stanza(SAMPLE_COREFILE, "home.")
        assert "\n\n\n" not in new_text


class TestUpdateCorefile:
    def test_writes_updated_file(self, tmp_path):
        f = tmp_path / "Corefile"
        f.write_text(SAMPLE_COREFILE)
        result = update_corefile(f, "home.")
        assert result is True
        assert "home.:53" not in f.read_text()

    def test_returns_false_when_stanza_absent(self, tmp_path):
        f = tmp_path / "Corefile"
        f.write_text(SAMPLE_COREFILE)
        result = update_corefile(f, "nonexistent.")
        assert result is False

    def test_no_tmp_file_left_behind(self, tmp_path):
        f = tmp_path / "Corefile"
        f.write_text(SAMPLE_COREFILE)
        update_corefile(f, "home.")
        assert not (tmp_path / "Corefile.tmp").exists()
