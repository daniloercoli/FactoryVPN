"""Static builds preserve display labels without changing public contracts."""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote
from xml.etree import ElementTree

import pytest

from scripts import build_website as website


class Page(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.title = ""
        self.in_title = False
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


@pytest.fixture(scope="module")
def raw_schema():
    return website.load_schema()


def snapshot(directory):
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_name_resolution_uses_only_the_public_setting_and_explicit_precedence(
    tmp_path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'APP_NAME="  Assistenza ${NOT_INTERPOLATED}  "\n'
        "DATABASE_URL=not-a-database-url\nJWT_SECRET=private-secret-sentinel\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("APP_NAME", raising=False)
    assert website.resolve_app_name(env_file=env_file) == "Assistenza ${NOT_INTERPOLATED}"
    monkeypatch.setenv("APP_NAME", "  Nome ambiente  ")
    assert website.resolve_app_name(env_file=env_file) == "Nome ambiente"
    assert website.resolve_app_name("  Nome esplicito  ", env_file) == "Nome esplicito"
    monkeypatch.delenv("APP_NAME")
    assert website.resolve_app_name(env_file=tmp_path / "missing.env") == "FactoryVPN"


@pytest.mark.parametrize("value", ["", "Nome\nnon valido"])
def test_invalid_selected_name_does_not_silently_fall_back(tmp_path, monkeypatch, value):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_NAME=Valido\n", encoding="utf-8")
    monkeypatch.setenv("APP_NAME", value)
    with pytest.raises(ValueError, match="APP_NAME"):
        website.resolve_app_name(env_file=env_file)


@pytest.mark.parametrize("label", ['<VPN> & "Team" $& $$ {{APP_INITIAL}}', "🛠 Assistenza"])
def test_label_is_literal_html_data_and_favicon_uses_its_first_unicode_character(label):
    rendered = website.render_page(
        '<title>{{APP_NAME}}</title><meta name="description" content="{{APP_NAME}}">'
        '<a aria-label="{{APP_NAME}}">{{APP_NAME}}</a>'
        '<link rel="icon" href="{{APP_FAVICON}}"><b>{{APP_INITIAL}}</b>',
        label,
    )
    page = Page(rendered)
    assert page.title == label
    assert [attrs["content"] for tag, attrs in page.tags if tag == "meta"] == [label]
    assert [attrs["aria-label"] for tag, attrs in page.tags if tag == "a"] == [label]
    assert [tag for tag, _ in page.tags] == ["title", "meta", "a", "link", "b"]
    icon = next(attrs["href"] for tag, attrs in page.tags if tag == "link")
    svg = ElementTree.fromstring(unquote(icon.split(",", 1)[1]))
    assert svg.find("{http://www.w3.org/2000/svg}text").text == label[0]


def test_complete_custom_build_keeps_protocols_and_excludes_deployment_data(monkeypatch):
    label = 'Assistenza & "Team" <script>bad()</script> $&'
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "not-a-database-url")
    monkeypatch.setenv("JWT_SECRET", "private-secret-sentinel")
    monkeypatch.setenv("APP_NAME", "Unrelated process label")
    files = website.build_files(label)
    required = {
        Path("index.html"), Path("developers.html"), Path("api/index.html"),
        Path("api/openapi.json"), Path("styles.css"), Path("developers.js"),
        Path("api/reference.js"), Path("vendor/swagger-ui/swagger-ui-bundle.js"),
    }
    assert required <= files.keys()
    for name in required:
        assert files[name]
    for relative, content in files.items():
        assert not {"src", "static", "project-notes", ".git"}.intersection(relative.parts)
        assert relative.name not in {".env", "AGENTS.md"}
        assert b"private-secret-sentinel" not in content
        assert b"Unrelated process label" not in content
        if relative.suffix == ".html":
            page = Page(content.decode())
            assert label in page.title
            assert all(attrs.get("src") for tag, attrs in page.tags if tag == "script")
    schema = json.loads(files[Path("api/openapi.json")])
    assert schema["info"]["title"] == f"{label} Customer API"
    assert (
        schema["components"]["securitySchemes"]["PortalCookie"]["name"]
        == "__Host-factoryvpn_portal"
    )
    developers = files[Path("developers.html")].decode()
    assert "https://vpn-api.example.com" in developers
    assert "X-FactoryVPN-Portal: 1" in developers
    assert 'download="factoryvpn-openapi.json"' in developers
    assert "https://factoryvpn.ercoliconsulting.eu/developers.html" in developers
    source = (website.WEBSITE / "src/developers.html").read_text(encoding="utf-8")
    assert set(re.findall(r"FACTORYVPN_[A-Z_]+", developers)) == set(
        re.findall(r"FACTORYVPN_[A-Z_]+", source)
    )


def test_rebuilding_and_checking_use_sources_and_never_rebrand_previous_output(
    tmp_path, monkeypatch, raw_schema
):
    monkeypatch.setattr(website, "load_schema", lambda: raw_schema)
    output = tmp_path / "public"
    assert website.build(output, "Alpha Assistance")
    alpha = snapshot(output)
    assert not website.build(output, "Beta Assistance", check=True)
    assert snapshot(output) == alpha
    assert website.build(output, "Beta Assistance")
    assert website.build(output, "Beta Assistance", check=True)
    for relative, content in snapshot(output).items():
        if relative.suffix == ".html":
            assert "Alpha Assistance" not in content.decode()
            assert "Beta Assistance" in Page(content.decode()).title
    schema = json.loads((output / "api/openapi.json").read_text(encoding="utf-8"))
    assert schema["info"]["title"] == "Beta Assistance Customer API"
    stylesheet = output / "styles.css"
    stylesheet.write_bytes(stylesheet.read_bytes() + b"/* stale */")
    stale = snapshot(output)
    assert not website.build(output, "Beta Assistance", check=True)
    assert snapshot(output) == stale


def test_existing_unrelated_files_are_preserved_and_block_publication(
    tmp_path, monkeypatch, raw_schema
):
    monkeypatch.setattr(website, "load_schema", lambda: raw_schema)
    output = tmp_path / "public"
    output.mkdir()
    note = output / "operator-notes.txt"
    note.write_text("private note", encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected files"):
        website.build(output, "Assistenza")
    assert snapshot(output) == {Path("operator-notes.txt"): b"private note"}


@pytest.mark.parametrize("source", ["src", "static/generated"])
def test_build_cannot_overwrite_its_source_tree(source):
    with pytest.raises(ValueError, match="separate from website sources"):
        website.build(website.WEBSITE / source, "Assistenza")
