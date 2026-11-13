"""Static guards for the 1.8.0 UI shell (index.html / app.css / app.js / sw.js).

These are cheap text-level checks for the classes of mistake a no-build-step frontend
makes easy: an icon referenced but never defined (an invisible button), a second inline
<script> (blocked by the nonce CSP), a theme offered in the picker with no palette, a
service worker that precaches stale files. Browser behaviour itself is covered by the
E2E suite (tests/e2e, module 15-shell).
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REPO = pathlib.Path(__file__).resolve().parent.parent
STATIC = REPO / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_every_referenced_icon_is_in_the_sprite():
    # <use href="#i-x"> against a missing <symbol> renders nothing — the control is
    # still there and clickable, just invisible. Both markup and JS-built markup count.
    html, js = _read("index.html"), _read("app.js")
    defined = set(re.findall(r'<symbol id="i-([\w-]+)"', html))
    used = set(re.findall(r'href="#i-([\w-]+)"', html))
    used |= set(re.findall(r"\bicon\('([\w-]+)'", js))
    used |= set(re.findall(r"#i-([\w-]+)", js))
    assert defined, "icon sprite missing from index.html"
    assert not (used - defined), f"icons referenced but not defined: {sorted(used - defined)}"
    # The sprite is inlined into every page load; don't let dead icons pile up in it.
    assert not (defined - used), f"icons defined but unused (prune in tools/gen-icons.js): {sorted(defined - used)}"


def test_icon_buttons_have_accessible_names():
    # Icon-only controls carry no text, so each needs an aria-label (the old UI relied
    # on title= alone, and on emoji that some platforms can't render).
    html = _read("index.html")
    buttons = re.findall(r'<button\b[^>]*class="[^"]*\b(?:icon-button|ctl-btn)\b[^"]*"[^>]*>', html)
    assert len(buttons) >= 15
    unnamed = [b for b in buttons if 'aria-label="' not in b]
    assert not unnamed, unnamed
    # No emoji / pictographs left in the shell markup.
    assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]", html)


def test_index_keeps_a_single_inline_script_and_no_inline_handlers():
    # The /piscope route stamps its CSP nonce onto the first bare <script>; a second
    # inline script, or an on*= attribute, would be blocked by script-src.
    html = _read("index.html")
    assert html.count("<script>") == 1
    assert not re.search(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", html), "inline event handler in index.html"
    # Same rule for markup built in app.js.
    assert not re.search(r"<[a-zA-Z][^>`]*\son(click|load|error|change|input)\s*=", _read("app.js"))


def test_default_theme_is_consistent_everywhere():
    from app.services import settings as settings_store
    default = settings_store.DEFAULTS["theme"]
    html, js, themes = _read("index.html"), _read("app.js"), _read("themes.css")
    assert f'<html lang="en" data-theme="{default}">' in html
    assert f"const DEFAULT_THEME = '{default}';" in js
    assert f'[data-theme="{default}"]' in themes


def test_every_theme_option_has_a_palette_and_default_tiles():
    html, js, themes = _read("index.html"), _read("app.js"), _read("themes.css")
    select = re.search(r'<select id="theme-select".*?</select>', html, re.DOTALL).group(0)
    options = re.findall(r'<option value="(\w+)"', select)
    assert len(options) == len(set(options)) >= 18
    tiles = re.search(r"const THEME_DEFAULT_TILES = \{(.*?)\};", js, re.DOTALL).group(1)
    presets = set(re.findall(r"^\s{2}(\w+):\s*\{ url:", js, re.MULTILINE))
    for theme in options:
        block = re.search(rf'\[data-theme="{theme}"\]\s*\{{([^}}]*)\}}', themes)
        assert block, f"theme {theme} has no palette block in themes.css"
        for token in ("--bg", "--panel-bg", "--surface-bg", "--primary-text", "--secondary-text",
                      "--accent", "--alert", "--success", "--warning", "--separator",
                      "--band-low", "--band-very-high", "--font-body",
                      "--map-tint", "--map-tone"):      # every theme styles the map too
            assert f"{token}:" in block.group(1), f"{theme} is missing {token}"
        m = re.search(rf"\b{theme}: '(\w+)'", tiles)
        assert m, f"theme {theme} has no default basemap"
        assert m.group(1) in presets, f"{theme} defaults to unknown tile preset {m.group(1)}"


def test_themes_are_told_apart_by_more_than_their_name():
    # The 1.9.0 pass exists because five dark themes were near-twins. Guard the cheapest
    # proxy for "looks the same": no two themes may share BOTH their accent colour and
    # their map treatment (tint + tone), and the picker groups them under headings.
    themes = _read("themes.css")
    seen: dict[tuple[str, ...], str] = {}
    for name, body in re.findall(r'\[data-theme="(\w+)"\]\s*\{([^}]*--map-tone[^}]*)\}', themes):
        key = tuple(re.search(rf"--{token}:\s*([^;]+);", body).group(1).strip()
                    for token in ("accent", "map-tint", "map-tone"))
        assert key not in seen, f"{name} and {seen[key]} share accent + map treatment"
        seen[key] = name
    assert len(seen) >= 18
    html = _read("index.html")
    select = re.search(r'<select id="theme-select".*?</select>', html, re.DOTALL).group(0)
    assert select.count("<optgroup ") == 2
    # "Automatic" basemaps that take the theme's tint must exist as presets.
    js = _read("app.js")
    for preset in ("themed", "themedLight"):
        assert re.search(rf"^\s{{2}}{preset}:\s*\{{ url:.*tint: 'theme'", js, re.MULTILINE), preset
    assert '#map[data-tile-filter="theme"] .leaflet-tile-pane { filter: var(--map-tint, none); }' in _read("app.css")


def test_theme_base_colours_stay_machine_parseable():
    # radar.js colorWithAlpha() and the polar chart parse these tokens; they must stay
    # plain rgb()/rgba()/#hex (no color-mix, hsl, named colours).
    themes = _read("themes.css")
    for name, body in re.findall(r'\[data-theme="(\w+)"\]\s*\{([^}]*)\}', themes):
        for token in ("accent", "accent-muted", "primary-text", "band-low", "band-mid", "band-high",
                      "band-very-high", "map-tone"):
            m = re.search(rf"--{token}:\s*([^;]+);", body)
            if m:
                assert re.match(r"^(#[0-9a-fA-F]{3,8}|rgba?\([\d\s.,]+\))$", m.group(1).strip()), (name, token, m.group(1))


def test_no_keyless_carto_basemaps():
    # basemaps.cartocdn.com answers keyless requests with an "API KEY REQUIRED" tile
    # (seen 2026-11); the presets were removed in 1.8.0. Don't reintroduce them.
    # (Match tile URLs, not the comment in app.js that explains the removal.)
    assert not re.search(r"url:\s*'https://[^']*cartocdn\.com", _read("app.js"))


def test_index_stamps_shell_tag_matching_service_worker(client):
    # The page tells its bootstrap which shell cache it was built for; it must be the
    # same tag /piscope/sw.js carries, or the stale-worker guard would purge on every load.
    page = client.get("/piscope").text
    m = re.search(r'<meta name="piscope-shell" content="(piscope-shell-[0-9_]+-[0-9a-f]+)" />', page)
    assert m, "shell tag not stamped into the served HTML"
    sw = client.get("/piscope/sw.js").text
    assert f"const CACHE = '{m.group(1)}';" in sw
    # The copy on the static mount is not stamped (and so never triggers the guard).
    assert '<meta name="piscope-shell" content="" />' in client.get("/piscope/static/index.html").text


def test_service_worker_cannot_serve_a_previous_release():
    # Found during the 1.8.0 redesign: (1) cache.addAll() went through the HTTP cache,
    # so a new worker could precache the previous release's CSS and serve it cache-first;
    # (2) caches.match() + ignoreSearch let a not-yet-replaced worker answer the new
    # page's ?v=<new> requests from its old cache.
    sw = _read("sw.js")
    assert "cache: 'reload'" in sw                      # precache bypasses the HTTP cache
    assert "caches.match(" not in sw                    # only ever read this worker's own cache
    assert "caches.open(CACHE).then((cache) => cache.match(" in sw
    assert "SHELL_VERSION" in sw and "v !== SHELL_VERSION" in sw   # step aside for another release
    # The version is recoverable from the rewritten tag for any semver.
    tag_re = re.compile(r"^piscope-shell-(\d+(?:_\d+)*)-")
    assert tag_re.match("piscope-shell-1_8_0-abc123").group(1).replace("_", ".") == "1.8.0"
    assert tag_re.match("piscope-shell-rewritten") is None
    # …and the page side of the fix: the bootstrap guard keyed on the stamped tag.
    html = _read("index.html")
    assert "piscope_shell_purged" in html and "unregister()" in html


def test_shell_precache_lists_only_real_files():
    # cache.addAll() rejects on the first 404, leaving the worker with an empty shell.
    sw = _read("sw.js")
    shell = re.search(r"const SHELL = \[(.*?)\];", sw, re.DOTALL).group(1)
    paths = re.findall(r"'/piscope/static/([^']+)'", shell)
    assert len(paths) >= 10
    for rel in paths:
        assert (STATIC / rel).is_file(), f"sw.js precaches a missing file: {rel}"


def test_manifest_and_favicon_point_at_existing_icons():
    manifest = json.loads(_read("manifest.webmanifest"))
    for icon in manifest["icons"]:
        rel = icon["src"].removeprefix("/piscope/static/")
        assert (STATIC / rel).is_file(), icon["src"]
    html = _read("index.html")
    for href in re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="/piscope/static/([^"]+)"', html):
        assert (STATIC / href).is_file(), href
    # The list-row heading glyph is a CSS mask of this file.
    assert "url('icons/aircraft.svg')" in _read("app.css")
    assert (STATIC / "icons" / "aircraft.svg").is_file()


def test_category_filter_select_is_still_the_source_of_truth():
    # The chips are sugar over #filter-category (the E2E suite and embed mode set the
    # select directly); every chip must map to one of its options.
    html = _read("index.html")
    select = re.search(r'<select id="filter-category".*?</select>', html, re.DOTALL).group(0)
    options = set(re.findall(r'<option value="(\w+)"', select))
    chips = set(re.findall(r'data-category="(\w+)"', html))
    assert chips and chips == options


def test_location_examples_use_the_public_placeholder():
    # The repo is public: every example coordinate shown to users (input placeholders,
    # validation messages) is the placeholder London receiver used by the mock feed and
    # the screenshots — never a real station position.
    html, js = _read("index.html"), _read("app.js")
    hints = re.findall(r'<input id="(?:wizard|setting-global)-(lat|lon)"[^>]*placeholder="e\.g\. ([-\d.]+)"', html)
    assert sorted(axis for axis, _ in hints) == ["lat", "lat", "lon", "lon"]
    for axis, value in hints:
        v = float(value)
        assert (51.0 <= v <= 52.0) if axis == "lat" else (-1.0 <= v <= 1.0), (axis, value)
    for example in re.findall(r"Decimal degrees, e\.g\. ([-\d.]+)\.", js):
        assert 51.0 <= float(example) <= 52.0, example
