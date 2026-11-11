"""WCAG contrast audit of every theme, on the colour pairs the UI actually renders.

Lighthouse only ever audits the active theme, and auditing raw palette tokens misses the
pairs that matter: app.css derives its surfaces by alpha-mixing (a tile is 5% of the text
colour over the panel; an active chip is accent text on a 15% accent wash) and derives
"text" variants of the accent/status colours via each theme's --accent-lift /
--status-lift. This test rebuilds those derived colours from static/themes.css (plus the
few per-theme overrides in static/app.css) and checks each pair for every theme in the
picker.

A theme that cannot meet a threshold without abandoning its canonical palette is listed
in KNOWN_EXCEPTIONS with the measured ratio's floor — the test also fails if an exception
stops being needed, so the list cannot rot.
"""
from __future__ import annotations

import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"

Color = tuple[float, float, float]
BLACK: Color = (0.0, 0.0, 0.0)
WHITE: Color = (255.0, 255.0, 255.0)


# --- minimal CSS reading ---------------------------------------------------------------

def _rules(css: str) -> list[tuple[list[str], dict[str, str]]]:
    """(selectors, custom-property declarations) for every flat rule, in source order."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [s.strip() for s in m.group(1).split(",")]
        decls = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(2)))
        if decls:
            out.append((selectors, {k: v.strip() for k, v in decls.items()}))
    return out


def _theme_tokens() -> dict[str, dict[str, str]]:
    """Custom properties per theme after the cascade: :root defaults, then every rule
    whose selector is exactly `[data-theme="x"]`, themes.css first, app.css after."""
    rules = _rules((STATIC / "themes.css").read_text(encoding="utf-8"))
    rules += _rules((STATIC / "app.css").read_text(encoding="utf-8"))
    base: dict[str, str] = {}
    themes: dict[str, dict[str, str]] = {}
    for selectors, decls in rules:
        for sel in selectors:
            if sel == ":root":
                base.update(decls)
            m = re.fullmatch(r'\[data-theme="(\w+)"\]', sel)
            if m:
                themes.setdefault(m.group(1), {}).update(decls)
    picker = re.search(r'<select id="theme-select".*?</select>',
                       (STATIC / "index.html").read_text(encoding="utf-8"), re.DOTALL).group(0)
    offered = re.findall(r'<option value="(\w+)"', picker)
    return {name: {**base, **themes[name]} for name in offered}


def _parse(value: str) -> Color:
    value = value.strip()
    m = re.match(r"rgba?\(([^)]*)\)", value)
    if m:
        r, g, b = (float(x) for x in re.split(r"[,\s/]+", m.group(1).strip())[:3])
        return (r, g, b)
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return (float(int(h[0:2], 16)), float(int(h[2:4], 16)), float(int(h[4:6], 16)))


def _lum(c: Color) -> float:
    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def _ratio(a: Color, b: Color) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _over(top: Color, alpha: float, bottom: Color) -> Color:
    return tuple(top[i] * alpha + bottom[i] * (1 - alpha) for i in range(3))  # type: ignore[return-value]


class Theme:
    def __init__(self, name: str, tokens: dict[str, str]):
        self.name, self.t = name, tokens

    def c(self, token: str) -> Color:
        return _parse(self.t[f"--{token}"])

    def pct(self, token: str) -> float:
        return float(self.t[f"--{token}"].rstrip("%")) / 100

    def solid(self, token: str, fallback: Color) -> Color:
        """A derived token: the theme's solid override if it has one, else the wash."""
        value = self.t.get(f"--{token}", "")
        return _parse(value) if re.match(r"^(#|rgb)", value) else fallback

    # derived colours, mirroring app.css
    @property
    def panel(self) -> Color: return self.c("panel-bg")
    @property
    def tile(self) -> Color: return self.solid("tile-bg", _over(self.c("primary-text"), 0.05, self.panel))
    @property
    def accent_soft(self) -> Color: return self.solid("accent-soft", _over(self.c("accent"), 0.15, self.panel))
    @property
    def ink(self) -> Color: return _parse(self.t["--contrast-ink"])
    def lifted(self, token: str, lift: str) -> Color: return _over(self.ink, self.pct(lift), self.c(token))
    @property
    def on_accent(self) -> Color: return self.solid("on-accent", self.c("bg"))
    @property
    def on_alert(self) -> Color: return self.solid("on-alert", BLACK)


# (label, minimum ratio, what it is in the UI, lambda theme -> (foreground, background))
CHECKS = [
    ("text/panel", 4.5, "body text", lambda t: (t.c("primary-text"), t.panel)),
    ("secondary/panel", 4.5, "labels and hints", lambda t: (t.c("secondary-text"), t.panel)),
    ("secondary/tile", 4.5, "stat-tile labels", lambda t: (t.c("secondary-text"), t.tile)),
    ("text/surface", 4.5, "status pill", lambda t: (t.c("primary-text"), t.c("surface-bg"))),
    ("accent-text/panel", 4.5, "links, section icons", lambda t: (t.lifted("accent", "accent-lift"), t.panel)),
    ("accent-text/soft", 3.0, "active tab / chip (bold)", lambda t: (t.lifted("accent", "accent-lift"), t.accent_soft)),
    ("on-accent/accent", 4.5, "primary button, WCH badge", lambda t: (t.on_accent, t.c("accent"))),
    ("on-alert/alert", 4.5, "EMG badge, error pill", lambda t: (t.on_alert, t.c("alert"))),
    ("black/warning", 4.5, "MIL badge", lambda t: (BLACK, t.c("warning"))),
    ("alert-text/panel", 4.5, "error text", lambda t: (t.lifted("alert", "status-lift"), t.panel)),
    ("success-text/panel", 4.5, "climb arrow, OK text", lambda t: (t.lifted("success", "status-lift"), t.panel)),
    ("warning-text/panel", 4.5, "descent arrow", lambda t: (t.lifted("warning", "status-lift"), t.panel)),
    ("tooltip", 4.5, "tooltip / toast (inverse)", lambda t: (t.c("bg"), t.c("primary-text"))),
]

# (theme, check) -> floor. Canonical palettes that cannot reach the threshold.
KNOWN_EXCEPTIONS: dict[tuple[str, str], float] = {
    # Solarized's text tones are low-contrast by design; base0 on a tile is the worst case.
    ("solarizedDark", "secondary/tile"): 4.0,
}

THEMES = {name: Theme(name, tokens) for name, tokens in _theme_tokens().items()}


def test_audit_covers_every_theme_in_the_picker():
    assert len(THEMES) >= 18
    for theme in THEMES.values():
        for token in ("bg", "panel-bg", "surface-bg", "primary-text", "secondary-text", "accent",
                      "alert", "success", "warning", "contrast-ink", "accent-lift", "status-lift"):
            assert f"--{token}" in theme.t, f"{theme.name} lacks --{token}"


@pytest.mark.parametrize("name", sorted(THEMES))
def test_theme_meets_contrast_thresholds(name: str):
    theme = THEMES[name]
    failures = []
    for label, minimum, what, pair in CHECKS:
        ratio = _ratio(*pair(theme))
        floor = KNOWN_EXCEPTIONS.get((name, label))
        if floor is not None:
            assert ratio < minimum, f"{name} {label} now passes ({ratio:.2f}) — drop it from KNOWN_EXCEPTIONS"
            minimum = floor
        if ratio < minimum:
            failures.append(f"{label} ({what}): {ratio:.2f} < {minimum}")
    assert not failures, f"{name}: " + "; ".join(failures)


def test_high_contrast_theme_is_aaa():
    # The theme exists for people who need it; its text pairs must clear 7:1.
    theme = THEMES["highContrast"]
    for label in ("text/panel", "secondary/panel", "secondary/tile", "text/surface",
                  "accent-text/panel", "on-accent/accent", "alert-text/panel",
                  "success-text/panel", "warning-text/panel", "tooltip"):
        _, _, _, pair = next(c for c in CHECKS if c[0] == label)
        assert _ratio(*pair(theme)) >= 7.0, f"highContrast {label} is {_ratio(*pair(theme)):.2f}"


def test_night_vision_stays_in_the_red_channel():
    # Red-light mode only works if nothing emits much green or blue. Every chrome and
    # traffic colour must be red-dominant; the single exception is --alert (yellow),
    # which is meant to break through.
    tokens = THEMES["nightVision"].t
    for token in ("bg", "panel-bg", "surface-bg", "primary-text", "secondary-text", "tertiary-text",
                  "accent", "accent-muted", "success", "warning", "separator", "map-receiver",
                  "aircraft-selected", "band-ground", "band-low", "band-mid", "band-high",
                  "band-very-high", "map-tone"):
        r, g, b = _parse(tokens[f"--{token}"])
        assert r >= g and r >= b, f"nightVision --{token} is not red-dominant: {tokens[f'--{token}']}"
        assert b <= 0.85 * max(r, 1), f"nightVision --{token} has too much blue: {tokens[f'--{token}']}"
