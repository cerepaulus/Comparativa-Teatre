"""Comparativa de taquilla Teatreneu (wip29).

Inicia sessió a app.wip29.com amb les credencials de l'usuari, descarrega les funcions
(una fila per funció) del període triat i les compara amb els anys anteriors.

    streamlit run app.py
"""
import json
import re
import time
import unicodedata
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup

LOGIN_URL = "https://app.wip29.com/login"
# El mateix que el botó "Excel detall funcions" de wip29: una fila per funció, per data de funció
EVENTS_URL = "https://app.wip29.com/ticketing/list/toExcelDetail"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MONTHS = ["gen", "febr", "març", "abr", "maig", "juny", "jul", "ag", "set", "oct", "nov", "des"]
WEEKDAYS = ["dl", "dt", "dc", "dj", "dv", "ds", "dg"]
GRANULARITIES = {"Dia": "D", "Setmana": "W", "Mes": "M", "Total del període": "T"}
PERIODS = ["Dates", "Setmana", "Any", "Temporada"]  # com els filtres de taquilla de wip29
TZ = "Europe/Madrid"
# Anys que es descarreguen: des del primer amb dades a wip29 fins a l'any vinent, que ja té entrades venudes
FIRST_YEAR, LAST_YEAR = 2022, pd.Timestamp.now(TZ).year + 1
MIN_DATE, MAX_DATE = date(FIRST_YEAR, 1, 1), date(LAST_YEAR, 12, 31)
# Setmanes que es poden triar: de la del primer dia amb dades (dilluns) a la de l'últim (diumenge)
WEEK_LIMITS = (MIN_DATE - timedelta(days=MIN_DATE.weekday()), MAX_DATE + timedelta(days=6 - MAX_DATE.weekday()))
ALL, ALL_CURRENT = "Tots", "Tots els actuals"  # opcions especials del selector d'espectacles
ALL_ROOMS = "Totes"  # opció del selector de sales
# Les dues sales del teatre, com sigui que les anomeni wip29 ("CT", "Sala Cafè Teatre"...).
# Clau: tros del nom en minúscules, sense accents, espais ni signes. Valor: (nom que es mostra, abreviació).
ROOMS = {"cafeteatre": ("Sala Cafè-Teatre", "CT"), "xavierfabregas": ("Sala Xavier Fàbregas", "XF")}
ROOM_CODES = dict(ROOMS.values())
IMPRO = "improshow"  # Impro Show (com la clau de SHOW_ALIASES), per comparar-lo amb la resta d'espectacles
REST = "Resta d'espectacles"

# Sufix de temporada al final del nom: "2022-2024", "24-25", "2025/26", "(2025)", "Temporada 25-26"
SEASON = re.compile(r"[\s(\-–·|,]*(?:\btemporada\b|\btemp\b\.?)?\s*"
                    r"(?:(?P<a>(?:20)?\d{2})\s*[-–/]\s*(?P<b>(?:20)?\d{2})|20\d{2})\s*\)?\s*$", re.I)
# Noms diferents del mateix espectacle que cap regla general no pot unir.
# Clau: tros del nom en minúscules, sense accents, espais ni signes. Valor: nom que es mostra.
SHOW_ALIASES = {
    "cleptomago": "Cleptómago - Magia con Shado",  # també "Shado presenta Cleptómago"
    "improshowsummer": "Impro Show",               # "Impro Show Summer Edition"
}

RAW_COLUMNS = ["shows", "paid", "invitation", "capacity", "amount", "commission"]
METRICS = {  # nom -> (unitat, càlcul a partir de les sumes de RAW_COLUMNS)
    "Recaptació": ("€", lambda t: t.amount),
    "Comissions": ("€", lambda t: t.commission),
    "Total (recaptació + comissions)": ("€", lambda t: t.amount + t.commission),
    "Entrades de pagament": ("", lambda t: t.paid),
    "Invitacions": ("", lambda t: t.invitation),
    "Espectadors totals": ("", lambda t: t.paid + t.invitation),
    "Funcions": ("", lambda t: t.shows),
    "Ocupació": ("%", lambda t: 100 * (t.paid + t.invitation) / t.capacity.where(t.capacity > 0)),
}

# Paleta validada per a daltonisme, en ordre fix: el període actual sempre és el blau. Tots els anys es veuen
# alhora (línies que es creuen, llegenda), així que l'ordre maximitza la distància entre qualsevol parell, no
# només entre veïns: en mode clar, blau, vermell, verd, groc i lila es distingeixen tots entre ells.
PALETTE_LIGHT = ["#2a78d6", "#e34948", "#008300", "#eda100", "#4a3aa7", "#eb6834", "#1baf7a"]
PALETTE_DARK = ["#3987e5", "#e66767", "#008300", "#c98500", "#9085e9", "#d95926", "#199e70"]

# Gràfics: colors del tema per defecte de Streamlit, perquè el marc del gràfic no es noti
CHART_PLOT = 300  # alçada de l'àrea de dibuix; el títol i les llegendes s'hi sumen
LEGEND_ROW = 20  # alçada d'una línia de llegenda vertical
CHART_FONT = '"Source Sans 3", "Source Sans Pro", system-ui, sans-serif'
CHART_THEMES = {False: dict(bg="#ffffff", text="#31333f", muted="#6b6f7e", grid="#e6eaf1"),
                True: dict(bg="#0e1117", text="#fafafa", muted="#a3a8b8", grid="#31333f")}
CHART_CONFIG = {"staticPlot": True, "responsive": True}
# Tooltip only while a finger/mouse is on a period: shown on touch or hover, hidden on lift,
# on leaving, or when a vertical swipe turns into page scrolling (pointercancel).
# The theme can change without the server knowing (Streamlit's settings menu), so the chart watches the
# page's background and swaps to the other theme's figure and colours when it changes.
CHART_TEMPLATE = """<!doctype html><meta charset="utf-8">
<style>
  :root { $CSS }
  html, body { margin: 0; overflow: hidden; background: var(--bg); font-family: $FONT; color: var(--text); }
  #wrap { position: relative; }
  #chart { touch-action: pan-y; }
  #band { position: absolute; display: none; pointer-events: none; background: var(--text); opacity: .08; }
  #tip { position: absolute; display: none; pointer-events: none; z-index: 1; background: var(--bg);
         border: 1px solid var(--grid); border-radius: 8px; padding: 6px 10px; font-size: 13px; line-height: 1.5;
         white-space: nowrap; box-shadow: 0 2px 8px rgba(0, 0, 0, .15); }
  #tip .h { font-weight: 600; margin-bottom: 2px; }
  #tip i { display: inline-block; box-sizing: border-box; width: 10px; height: 10px; border: 2px solid;
           border-radius: 2px; margin-right: 6px; }
  #tip span { color: var(--muted); font-size: 11px; }
</style>
<div id="wrap">$PLOT<div id="band"></div><div id="tip"></div></div>
<script>
const TIPS = $TIPS, FIGS = $FIGS, THEMES = $THEMES, gd = document.getElementById("chart");
let dark = $DARK;
function pageIsDark() {
  try {
    const doc = window.parent.document;
    for (const el of [doc.querySelector(".stApp"), doc.body]) {
      const [r, g, b, a = 1] = ((el && getComputedStyle(el).backgroundColor.match(/[\\d.]+/g)) || []).map(Number);
      if (r !== undefined && a > 0) return .299 * r + .587 * g + .114 * b < 128;
    }
  } catch (e) {}  // the page can't be read: keep the server's guess
  return dark;
}
function follow() {
  if (pageIsDark() === dark) return;
  dark = !dark;
  for (const [k, v] of Object.entries(THEMES[+dark])) document.documentElement.style.setProperty(k, v);
  Plotly.react(gd, FIGS[+dark].data, FIGS[+dark].layout, $CONFIG);
}
follow();
setInterval(follow, 500);
const tip = document.getElementById("tip"), band = document.getElementById("band");
function hide() { tip.style.display = band.style.display = "none"; }
function show(e) {
  const s = gd._fullLayout && gd._fullLayout._size;
  const x = s ? e.clientX - gd.getBoundingClientRect().left - s.l : -1;
  if (x < 0 || x >= s.w) return hide();
  const w = s.w / TIPS.length, i = Math.floor(x / w);
  band.style.cssText = `display:block;left:${s.l + i * w}px;width:${w}px;top:${s.t}px;height:${s.h}px`;
  tip.innerHTML = TIPS[i];
  tip.style.display = "block";
  const left = s.l + (i + .5) * w - tip.offsetWidth / 2;
  tip.style.left = Math.max(0, Math.min(left, gd.clientWidth - tip.offsetWidth)) + "px";
  tip.style.top = s.t + "px";
}
gd.addEventListener("pointerdown", show);
gd.addEventListener("pointermove", e => (e.pointerType === "mouse" || e.buttons) && show(e));
gd.addEventListener("pointerup", e => e.pointerType !== "mouse" && hide());
gd.addEventListener("pointercancel", hide);
gd.addEventListener("pointerleave", hide);
</script>"""


class SessionExpired(Exception):
    pass


# ---------------------------------------------------------------- wip29

def login(email: str, password: str) -> requests.Session | None:
    """Returns an authenticated session, or None if wip29 rejects the credentials."""
    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT
    page = http.get(LOGIN_URL, timeout=20)
    page.raise_for_status()
    token = BeautifulSoup(page.text, "html.parser").find("input", {"name": "_token"})
    if token is None:
        raise RuntimeError("no s'ha trobat el formulari d'inici de sessió")
    resp = http.post(LOGIN_URL, timeout=20, headers={"Referer": LOGIN_URL},
                     data={"_token": token["value"], "email": email, "password": password})
    return http if resp.ok and not resp.url.rstrip("/").endswith("/login") else None


def barcelona_today() -> date:
    """Barcelona's date: the server (e.g. Streamlit Cloud) may run on UTC."""
    return pd.Timestamp.now(TZ).date()


def season_year(d: date) -> int:
    """Year its wip29 season (1 September - 31 August) starts: 6/10/2026 -> 2026, 6/3/2026 -> 2025."""
    return d.year - (d.month < 9)


@st.cache_data(ttl="1d", max_entries=100, show_spinner=False)
def fetch_year(_http: requests.Session, email: str, year: int, loaded_at: float) -> list[dict]:
    """Every performance of one calendar year. `email` keys the cache, so accounts never share it;
    a new `loaded_at` forces a new download."""
    resp = _http.get(EVENTS_URL, timeout=180, allow_redirects=False,
                     params={"dates[]": [f"{year}-01-01", f"{year}-12-31"]})
    if resp.is_redirect or resp.status_code in (401, 419):  # wip29 redirigeix a /login
        raise SessionExpired
    resp.raise_for_status()
    return resp.json()["events"]


def squash(name: str) -> str:
    """'Monólogos & Vermut.' -> 'monologosvermut': ignores case, accents, spaces and punctuation."""
    return "".join(c for c in unicodedata.normalize("NFKD", name.casefold()) if c.isalnum())


def show_name(activity: str) -> str:
    """Seasons of the same show count as one show: 'Impro Show 25-26' -> 'Impro Show'."""
    # only real seasons ("24-25", "2022-2024"), not titles like "los 80/90"
    real = lambda m: not m["a"] or 0 < (int(m["b"][-2:]) - int(m["a"][-2:])) % 100 <= 3
    name = " ".join(SEASON.sub(lambda m: "" if real(m) else m[0], activity).split()).strip(" ./-–·,") or activity
    return next((alias for part, alias in SHOW_ALIASES.items() if part in squash(name)), name)


def room_name(*names) -> str:
    """The room of a performance from the names wip29 gives it (`theater`, `calendar`): one of ROOMS if any
    of them is one, in whatever spelling; otherwise the first name given."""
    names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    for key in map(squash, names):
        for part, (room, code) in ROOMS.items():
            if part in key or key in (code.lower(), "sala" + code.lower()):
                return room
    return names[0] if names else "Sense sala"


@st.cache_data(ttl="1d", max_entries=20, show_spinner=False)
def load_events(_http: requests.Session, email: str, loaded_at: float) -> pd.DataFrame:
    """Every performance from FIRST_YEAR to LAST_YEAR: one request per year, in parallel.
    Cached, so redraws never reprocess. A new `loaded_at` (login, "Actualitza") downloads the
    current and future years again; past years come from fetch_year's cache."""
    years = range(FIRST_YEAR, LAST_YEAR + 1)
    with ThreadPoolExecutor(len(years)) as pool:
        this_year = barcelona_today().year
        batches = pool.map(lambda y: fetch_year(_http, email, y, loaded_at if y >= this_year else 0), years)
        events = [e for batch in batches for e in batch]
    df = pd.DataFrame(events, columns=["id", "activity", "start", "theater", "calendar", *RAW_COLUMNS[1:]]
                      ).drop_duplicates("id")
    numbers = df[RAW_COLUMNS[1:]].apply(pd.to_numeric, errors="coerce")
    unreadable = int((numbers.isna() & df[RAW_COLUMNS[1:]].notna()).sum().sum())  # sent, but not a number
    df[RAW_COLUMNS[1:]] = numbers.fillna(0)
    # Variants of one name ("Los Hijos" / "Los Hijos.") become its most common spelling
    names = df["activity"].map(show_name)
    keys = names.map(squash)
    spelling = names.groupby(keys).agg(lambda s: s.value_counts().index[0])
    # A performance without a single ticket (cancelled, or not sold yet) is not a funció and its seats don't count
    # for occupancy; its money, if any, still does
    sold = df["paid"] + df["invitation"] > 0
    df = df.assign(shows=sold.astype(int), capacity=df["capacity"].where(sold, 0), activity=keys.map(spelling),
                   room=list(map(room_name, df["theater"], df["calendar"])),
                   day=pd.to_datetime(df["start"]).dt.normalize()).drop(columns=["id", "start", "theater", "calendar"])
    df.attrs["unreadable"] = unreadable
    return df


# ---------------------------------------------------------------- periods

def shift_back(d: date, years: int, is_end: bool = False) -> date:
    """Same date N years earlier."""
    y = d.year - years
    last = monthrange(y, d.month)[1]
    if is_end and d.day == monthrange(d.year, d.month)[1]:
        return date(y, d.month, last)  # final de mes -> final de mes (febrers de traspàs)
    return date(y, d.month, min(d.day, last))


def shift(start: date, end: date, years: int, gran: str) -> tuple[date, date]:
    """The same period N years earlier. By days or weeks it moves back whole weeks, so weekdays line up: as many
    as land nearest the same dates (at most 3 days off, however many years back). Otherwise, the same dates."""
    if gran in ("D", "W"):
        back = timedelta(weeks=round((start - shift_back(start, years)).days / 7))
        return start - back, end - back
    return shift_back(start, years), shift_back(end, years, is_end=True)


def split(start: date, end: date, gran: str) -> list[tuple[date, date]]:
    if gran == "T":
        return [(start, end)]
    buckets, cur = [], start
    while cur <= end:
        stop = {"D": cur,
                "W": cur + timedelta(days=6 - cur.weekday()),
                "M": date(cur.year, cur.month, monthrange(cur.year, cur.month)[1])}[gran]
        stop = min(stop, end)
        buckets.append((cur, stop))
        cur = stop + timedelta(days=1)
    return buckets


def bucket_label(d: date, gran: str, with_year: bool) -> str:
    year = f" {d:%y}" if with_year else ""
    return {"D": f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}{year}",
            "W": f"{d.day} {MONTHS[d.month - 1]}{year}",
            "M": f"{MONTHS[d.month - 1]}{year}",
            "T": "Total"}[gran]


def plan(start: date, end: date, gran: str, years_back: int, season: bool = False) -> pd.DataFrame:
    """One row per (year offset, bucket): its dates and how to label it. A season is always named
    as one ("2026–2027"), even when cut short at today."""
    base = split(start, end, gran)
    rows = []
    for k in range(years_back + 1):
        a, b = start.year - k, (start.year + 1 if season else end.year) - k
        shifted = split(*shift(start, end, k, gran), gran)
        for idx, ((bs, _), (s, e)) in enumerate(zip(base, shifted)):
            rows.append({"offset": k, "idx": idx, "start": s, "end": e,
                         "series": str(a) if a == b else f"{a}–{b}",
                         "label": bucket_label(bs, gran, start.year != end.year),
                         "dates": f"{s:%d/%m/%Y}" if s == e else f"{s:%d/%m/%Y} – {e:%d/%m/%Y}"})
    return pd.DataFrame(rows)


def bucketize(events: pd.DataFrame, buckets: pd.DataFrame) -> pd.DataFrame:
    """Sums of RAW_COLUMNS per bucket, in `buckets` order; empty buckets are zeros."""
    bounds = buckets[["offset", "idx", "start", "end"]].astype({"start": "datetime64[ns]", "end": "datetime64[ns]"})
    events = events.astype({"day": "datetime64[ns]"}).sort_values("day")
    parts = [m[m["day"] <= m["end"]] for _, b in bounds.groupby("offset")
             for m in [pd.merge_asof(events, b, left_on="day", right_on="start")]]
    sums = pd.concat(parts).groupby(["offset", "idx"])[RAW_COLUMNS].sum()
    return sums.reindex(pd.MultiIndex.from_frame(buckets[["offset", "idx"]]), fill_value=0)


# ---------------------------------------------------------------- presentation

def fmt(value: float, unit: str) -> str:
    if pd.isna(value):
        return "–"
    s = f"{value:,.{2 if unit else 0}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {unit}".strip()


def metric_table(sums: pd.DataFrame, names: list[str]) -> pd.DataFrame:
    return pd.DataFrame({m: METRICS[m][1](sums) for m in names}, index=sums.index)


def change(now: float, then: float, unit: str) -> str:
    """' · ▲ +12,3 %' in green or ' · ▼ -4,5 %' in red: how `now` compares with `then`. A percentage (occupancy)
    changes in points: 50 % -> 55 % is +5 points, not +10 %."""
    if not then or pd.isna(then) or pd.isna(now):
        return ""
    diff = now - then if unit == "%" else (now - then) / abs(then) * 100
    arrow, color = ("▲", "green") if diff >= 0 else ("▼", "red")
    return f" · :{color}[{arrow} {diff:+.1f} {'punts' if unit == '%' else '%'}]".replace(".", ",")


def uses_lines(table: pd.DataFrame) -> bool:
    """Too many bars to read (periods × groups of shows > 16): a line chart."""
    periods = table["label"].nunique() if table["label"].nunique() > 1 else table["series"].nunique()
    return periods * table["group"].nunique() > 16


def figure(table: pd.DataFrame, metric: str, dark: bool) -> go.Figure:
    """One metric's chart in one theme: a colour per year, the current one first in the palette. With two groups
    of shows (Impro Show and the rest) the second is striped (bars) or dashed with empty circles (lines), and the
    legend has two columns: the years by colour, and the groups by their look, in the colour of the text."""
    unit, theme = METRICS[metric][0], CHART_THEMES[dark]
    series = list(dict.fromkeys(table.sort_values("offset")["series"]))  # newest first, as in the cards
    color = dict(zip(series, PALETTE_DARK if dark else PALETTE_LIGHT))
    groups = list(dict.fromkeys(table["group"]))
    two = len(groups) > 1
    x = "series" if table["label"].nunique() == 1 else "label"
    periods = series[::-1] if x == "series" else list(dict.fromkeys(table["label"]))
    lines = uses_lines(table)
    markers = lines and len(periods) <= 60
    # side by side per year and group; with a bar per year ("Total del període"), per group only
    slot = lambda s, g: g if x == "series" else f"{s}{g}"
    stripes = lambda g, c: dict(color=c, pattern=dict(shape="/" if g else "", fillmode="overlay", solidity=0.5))
    fig = go.Figure()
    for s in series[::-1]:  # oldest first, from left to right
        for g, group in enumerate(groups):
            y = table[(table["series"] == s) & (table["group"] == group)].set_index(x)[metric].reindex(periods)
            empty, c = g > 0, color[s]
            if lines:
                fig.add_scatter(x=periods, y=y, name=s, showlegend=not two, mode="lines+markers" if markers else "lines",
                                line=dict(color=c, width=2, dash="dash" if empty else "solid"),
                                marker=dict(color=c, size=8, symbol="circle-open" if empty else "circle",
                                            line_width=2 if empty else 0))
            else:
                fig.add_bar(x=periods, y=y, name=s, showlegend=not two, offsetgroup=slot(s, g), marker=stripes(g, c))
    if two:  # data traces stay out of the legend: two columns of entries without data instead
        for s in series:
            fig.add_scatter(x=[None], y=[None], name=s, mode="markers", legend="legend",
                            marker=dict(color=color[s], size=12, symbol="square"))
        for g, group in enumerate(groups):
            if lines:
                fig.add_scatter(x=[None], y=[None], name=group, mode="lines+markers", legend="legend2",
                                line=dict(color=theme["text"], width=2, dash="dash" if g else "solid"),
                                marker=dict(color=theme["text"], size=10, symbol="circle-open" if g else "circle",
                                            line_width=2 if g else 0))
            else:  # a bar without data, in a slot that exists already, so it moves no bar: its legend entry shows
                fig.add_bar(x=[None], y=[None], name=group, legend="legend2", offsetgroup=slot(series[0], g),
                            marker=stripes(g, theme["text"]))
    top = 40 + (max(len(series), len(groups)) * LEGEND_ROW if two else 24) + 8  # title, legend
    height = CHART_PLOT + top + 8
    legend = dict(title=None, orientation="v" if two else "h", x=0, xanchor="left", y=1 - 40 / height,
                  yref="container", yanchor="top", tracegroupgap=0)
    fig.update_layout(
        template="plotly_dark" if dark else "plotly_white", paper_bgcolor=theme["bg"], plot_bgcolor=theme["bg"],
        font=dict(family=CHART_FONT, color=theme["text"]), separators=",.", height=height,
        xaxis=dict(type="category", categoryorder="array", categoryarray=periods, title=None, automargin=True),
        yaxis=dict(title=None, ticksuffix=f" {unit}" if unit else "", gridcolor=theme["grid"], automargin=True),
        # title on top, legend under it (left-aligned), so a long title never collides with the legend
        title=dict(text=metric, x=0, xref="paper", y=1, yref="container", yanchor="top", pad=dict(t=8)),
        legend=legend, **(dict(legend2=legend | dict(x=0.5)) if two else {}),
        barmode="group", bargap=0.25, bargroupgap=0.06, barcornerradius=4, margin=dict(l=8, r=8, t=top, b=8),
    )
    return fig


def chart(table: pd.DataFrame, metric: str, dark: bool) -> tuple[str, int]:
    """A static chart (no zoom, scroll capture or toolbar) as HTML, and its height, plus a tooltip with every
    year's value for a period that shows only while the finger or mouse is on that period. It carries both
    themes and follows the page's (`dark` is only the server's guess, for the first paint)."""
    unit = METRICS[metric][0]
    figs = [figure(table, metric, d) for d in (False, True)]
    series = list(dict.fromkeys(table.sort_values("offset")["series"]))
    groups = list(dict.fromkeys(table["group"]))
    x = "series" if table["label"].nunique() == 1 else "label"
    # one tooltip per period, newest year first as in the cards; the second group's square as in the chart:
    # empty (lines) or striped (bars)
    rest = "transparent" if uses_lines(table) else "repeating-linear-gradient(135deg, {c} 0 2px, transparent 2px 4px)"
    rows = {}
    for r in table.sort_values(["offset", "g"]).to_dict("records"):
        c = f"var(--s{series.index(r['series'])})"
        rows.setdefault(r[x], []).append(
            f'<div><i style="border-color:{c};background:{rest.format(c=c) if r["g"] else c}"></i>{r["series"]}'
            f'{" · " + r["group"] if len(groups) > 1 else ""} '
            f'<b>{fmt(r[metric], unit)}</b> <span>{r["dates"]}</span></div>')
    periods = series[::-1] if x == "series" else list(dict.fromkeys(table["label"]))
    tips = [f'<div class="h">{period}</div>' + "".join(rows[period]) for period in periods]
    themes = [{f"--{k}": v for k, v in CHART_THEMES[d].items()}
              | {f"--s{i}": c for i, c in enumerate(PALETTE_DARK if d else PALETTE_LIGHT)} for d in (False, True)]
    plot = figs[dark].to_html(full_html=False, include_plotlyjs="cdn", div_id="chart", config=CHART_CONFIG)
    script_safe = lambda s: s.replace("</", "<\\/")  # nothing inside a <script> may close it
    html = (CHART_TEMPLATE.replace("$CSS", "; ".join(f"{k}: {v}" for k, v in themes[dark].items()))
            .replace("$FONT", CHART_FONT).replace("$PLOT", plot).replace("$DARK", json.dumps(dark))
            .replace("$CONFIG", json.dumps(CHART_CONFIG)).replace("$THEMES", json.dumps(themes))
            .replace("$FIGS", script_safe(f"[{figs[0].to_json()}, {figs[1].to_json()}]"))
            .replace("$TIPS", script_safe(json.dumps(tips))))
    return html, figs[dark].layout.height


def reset_if_all() -> None:
    """Picking "Tots" clears the selection: back to every show, as on first load."""
    if ALL in st.session_state.shows:
        st.session_state.shows = []


def logout(message: str | None = None) -> None:
    st.session_state.clear()
    if message:
        st.session_state.flash = message
    st.rerun()


# ---------------------------------------------------------------- pages

def login_page() -> None:
    st.title("Comparativa de taquilla")
    if msg := st.session_state.pop("flash", None):
        st.warning(msg)
    with st.form("login"):
        email = st.text_input("Correu electrònic", autocomplete="username")
        password = st.text_input("Contrasenya", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Inicia sessió", type="primary", width="stretch")
    st.caption("Les credencials s'envien només a app.wip29.com i no es desen enlloc.")
    if not submitted:
        return
    with st.spinner("Iniciant sessió…"):
        try:
            http = login(email.strip(), password)
        except (requests.RequestException, RuntimeError) as exc:
            st.error(f"No s'ha pogut connectar amb wip29: {exc}")
            return
    if http is None:
        st.error("Correu o contrasenya incorrectes.")
        return
    st.session_state.http = http
    st.session_state.email = email.strip()
    st.rerun()


def snap_week(key: str) -> None:
    """Picking any day in "Des de" or "Fins a" picks its whole week: the Monday and the Sunday."""
    day = st.session_state[key]
    monday = day - timedelta(days=day.weekday())
    st.session_state.week_start, st.session_state.week_end = monday, monday + timedelta(days=6)


def step_week(weeks: int) -> None:
    monday = st.session_state.week_start + timedelta(weeks=weeks)
    if WEEK_LIMITS[0] <= monday <= WEEK_LIMITS[1] - timedelta(days=6):
        st.session_state.week_start, st.session_state.week_end = monday, monday + timedelta(days=6)


def pick_period(period: str, today: date, c1, c2) -> tuple[date, date]:
    """First and last day of the period picked as in wip29: a date range (that "Fins avui" ends today),
    a week (Monday to Sunday), a month, a natural year or a season."""
    limits = dict(min_value=MIN_DATE, max_value=MAX_DATE, format="DD/MM/YYYY")
    if period == "Dates":
        start = c1.date_input("Des de", date(today.year, 1, 1), **limits)
        end_box = c2.container()  # the toggle decides the date input, but sits below it
        until_today = c2.toggle("Fins avui")
        end = end_box.date_input("Fins a", today, **limits, disabled=until_today, key=f"end_{until_today}")
        # always the current date, even if the app stays open past midnight
        return start, today if until_today else end
    if period == "Setmana":  # el calendari no pot amagar dies: qualsevol dia tria la seva setmana sencera
        monday = today - timedelta(days=today.weekday())
        st.session_state.setdefault("week_start", monday)
        st.session_state.setdefault("week_end", monday + timedelta(days=6))
        week = dict(min_value=WEEK_LIMITS[0], max_value=WEEK_LIMITS[1], format="DD/MM/YYYY", on_change=snap_week)
        start = c1.date_input("Des de", key="week_start", args=("week_start",), **week)
        end = c2.date_input("Fins a", key="week_end", args=("week_end",), **week)
        c1.button("◀", on_click=step_week, args=(-1,), width="stretch")
        c2.button("▶", on_click=step_week, args=(1,), width="stretch")
        return max(start, MIN_DATE), end  # la setmana de l'1/1/2022 comença el 2021, sense dades
    if period == "Any":
        year = c1.selectbox("Any", range(FIRST_YEAR, LAST_YEAR + 1), index=today.year - FIRST_YEAR)
        start, end = date(year, 1, 1), date(year, 12, 31)
    else:
        # only seasons that end within the downloaded years
        year = c1.selectbox("Temporada", range(FIRST_YEAR, LAST_YEAR), index=season_year(today) - FIRST_YEAR,
                            format_func=lambda y: f"{y}/{y + 1}")
        start, end = date(year, 9, 1), date(year + 1, 8, 31)
    return start, end


def dashboard() -> None:
    head, refresh, out = st.columns([4, 1, 1], vertical_alignment="bottom")
    head.title("Comparativa de taquilla")
    if out.button("Surt", width="stretch"):
        logout()
    # Tot es descarrega en iniciar sessió; després només amb "Actualitza" (anys passats: memòria d'1 dia)
    if refresh.button("Actualitza", width="stretch", help="Torna a descarregar l'any actual i els següents"):
        st.session_state.loaded_at = time.time()
    loaded_at = st.session_state.setdefault("loaded_at", time.time())
    loaded_time = pd.Timestamp(loaded_at, unit="s", tz="UTC").tz_convert(TZ)
    st.caption(f"Sessió iniciada com a {st.session_state.email} · dades de les {loaded_time:%H:%M}")

    try:
        with st.spinner(f"Descarregant totes les funcions de wip29 ({FIRST_YEAR}–{LAST_YEAR})…"):
            events = load_events(st.session_state.http, st.session_state.email, loaded_at)
    except SessionExpired:
        logout("La sessió de wip29 ha caducat. Torna a iniciar sessió.")
    except (requests.RequestException, ValueError, KeyError) as exc:
        st.error(f"Error en descarregar les dades de wip29: {exc}")
        return

    if unreadable := events.attrs.get("unreadable"):
        st.warning(f"wip29 ha enviat {unreadable} xifres que no són números: compten com a 0, i els totals poden "
                   "ser incorrectes.")
    today = barcelona_today()
    top_left, top_right = st.columns([3, 2])
    period = top_left.segmented_control("Període", PERIODS, default=PERIODS[0], required=True, key="period")
    rooms = sorted(set(events["room"]), key=lambda r: (r not in ROOM_CODES, r))  # CT i XF primer
    room = top_right.segmented_control("Sala", [ALL_ROOMS, *rooms], default=ALL_ROOMS, required=True, key="room",
                                       format_func=lambda r: ROOM_CODES.get(r, r)) if len(rooms) > 1 else ALL_ROOMS
    c1, c2, c3, c4 = st.columns(4)
    start, end = pick_period(period, today, c1, c2)
    # Cada tipus de període recorda la seva agrupació; una setmana, per defecte per dies
    gran = GRANULARITIES[c3.selectbox("Agrupa per", list(GRANULARITIES), key=f"gran_{period}",
                                      index=0 if period == "Setmana" else 2)]
    # Actuals: amb alguna funció a la temporada de wip29 en curs (1 de setembre - 31 d'agost)
    season_start = pd.Timestamp(season_year(today), 9, 1)
    current = set(events.loc[events["day"].between(season_start, season_start + pd.DateOffset(years=1, days=-1)),
                             "activity"])
    past = set(events["activity"]) - current
    impro_name = next((s for s in current | past if squash(s) == IMPRO), "Impro Show")
    impro = st.toggle(f"{impro_name} vs resta", key="impro")
    # Only years whose whole period has data: wip29's records start on the first day with a performance, and a
    # period starting earlier would show its missing days as zeros, as if nothing had been sold
    first_day = events["day"].min().date() if len(events) else MIN_DATE
    max_back = max((k for k in range(1, len(PALETTE_LIGHT)) if shift(start, end, k, gran)[0] >= first_day),
                   default=0)
    # At the maximum until the user moves it; then their choice, capped to what fits
    chosen = st.session_state.get("years_back") if st.session_state.get("years_moved") else max_back
    st.session_state["years_back"] = min(chosen, max_back)
    years_back = st.slider("Anys anteriors a comparar", 0, max_back, key="years_back", width=240,
                           on_change=lambda: st.session_state.update(years_moved=True)) if max_back else 0
    if start > end:
        st.error("La data d'inici és posterior a la data final.")
        return
    options = [ALL, ALL_CURRENT, *sorted(current, key=squash), *sorted(past, key=squash)]
    picked = c4.multiselect("Espectacle", options, key="shows", on_change=reset_if_all, select_all=False,
                            format_func=lambda s: f"{s} (antic)" if s in past else s, placeholder=ALL, disabled=impro)
    exclude = c4.toggle("Exclou els triats", disabled=not picked or impro)
    shows = set(picked) - {ALL_CURRENT} | (current if ALL_CURRENT in picked else set())
    if room != ALL_ROOMS:
        events = events[events["room"] == room]
    if impro:  # dues parts de cada període: Impro Show i la resta d'espectacles
        is_impro = events["activity"].map(squash) == IMPRO
        groups = {impro_name: events[is_impro], REST: events[~is_impro]}
        title = f"{impro_name} vs resta d'espectacles"
    else:
        groups = {"": events[events["activity"].isin(shows) != exclude] if picked else events}
        title = "Tots els espectacles" if not picked else ("Tots excepte " if exclude else "") + ", ".join(picked)
    buckets = plan(start, end, gran, years_back, season=period == "Temporada")
    show_results(buckets, {name: bucketize(group, buckets) for name, group in groups.items()},
                 title + ("" if room == ALL_ROOMS else f" · {room}"))


def show_results(buckets: pd.DataFrame, parts: dict[str, pd.DataFrame], title: str) -> None:
    """Cards, charts and table, one series per year. `parts` holds the sums of each group of shows: one group,
    or two (Impro Show and the rest), and then the cards give the first one's figures and its share of the total."""
    st.divider()
    st.subheader(title)
    if not any(sums["shows"].any() for sums in parts.values()):
        st.warning("No hi ha dades d'aquests espectacles en aquest període.")
        return
    metrics = st.multiselect("Mètriques", list(METRICS), default=["Recaptació", "Espectadors totals"])
    if not metrics:
        st.info("Tria almenys una mètrica.")
        return

    series = list(dict.fromkeys(buckets["series"]))  # ordenat per offset: actual primer
    dark = getattr(getattr(st.context, "theme", None), "type", None) == "dark"  # first guess; the charts check
    main, *others = parts

    table = pd.concat([buckets.join(metric_table(sums, metrics).reset_index(drop=True)).assign(group=name, g=g)
                       for g, (name, sums) in enumerate(parts.items())], ignore_index=True)

    # Xifres clau: el període triat en gran i, a sota, cada any anterior amb la variació respecte a ell
    # (i, si es compara Impro Show amb la resta, la part del total de cada any que és seva)
    totals = {name: metric_table(sums.groupby(level="offset").sum(), metrics) for name, sums in parts.items()}
    points = " (l'ocupació, en punts)" if "Ocupació" in metrics else ""
    if others:
        of = "d'" if main[:1].lower() in "aeiouhàèéíòóú" else "de "
        st.caption(f"Les xifres grans són {of}{main}; «del total» és la seva part del total" + (
            f", i les fletxes, la variació de {series[0]} respecte a cada any{points}." if len(series) > 1 else "."))
    elif len(series) > 1:
        st.caption(f"Els percentatges són la variació de {series[0]} respecte a cada any{points}.")
    cols = []
    for i, m in enumerate(metrics):
        unit, now = METRICS[m][0], totals[main].loc[0, m]
        whole = sum(t[m] for t in totals.values())  # per any
        share = lambda k: (f" · {totals[main].loc[k, m] / whole.loc[k] * 100:.1f} % del total".replace(".", ",")
                           if others and unit != "%" and whole.loc[k] else "")  # l'ocupació no se suma
        lines = [f"**{name}** · {fmt(totals[name].loc[0, m], unit)}" for name in others]
        if share(0):
            lines.append(f"**{main}**{share(0)}")
        for k in range(1, len(series)):
            then = totals[main].loc[k, m]
            lines.append(f"**{series[k]}** · {fmt(then, unit)}{change(now, then, unit)}{share(k)}" + "".join(
                f" · resta {fmt(t.loc[k, m], unit)}{change(t.loc[0, m], t.loc[k, m], unit)}"
                for t in map(totals.get, others)))
        per_row = 1 if others else 2  # Impro Show vs resta: lines too long for half a row
        if i % per_row == 0:
            cols = st.columns(per_row)  # each row its own columns so rows line up
        with cols[i % per_row].container(border=True):
            st.metric(f"{m} · {main + ' · ' if others else ''}{series[0]}", fmt(now, unit))
            if lines:
                st.caption("  \n".join(lines))

    for m in metrics:
        html, height = chart(table, m, dark)
        st.iframe(html, height=height)

    with st.expander("Taula de dades"):
        chronological = table.sort_values(["offset", "g", "idx"], ascending=[False, True, True])
        columns = ["series", *(["group"] if others else []), "label", "dates", *metrics]
        st.dataframe(chronological[columns].rename(
            columns={"series": "Any", "group": "Espectacles", "label": "Període", "dates": "Dates"}),
            width="stretch", hide_index=True)


if __name__ == "__main__":
    st.set_page_config(page_title="Comparativa de taquilla · Teatreneu", page_icon="🎭")
    if "http" in st.session_state:
        dashboard()
    else:
        login_page()
