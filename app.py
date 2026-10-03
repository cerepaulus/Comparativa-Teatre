"""Comparativa de taquilla Teatreneu (wip29).

Inicia sessió a app.wip29.com amb les credencials de l'usuari, descarrega les funcions
(una fila per funció) del període triat i les compara amb els anys anteriors.

    streamlit run app.py
"""
import re
import time
import unicodedata
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
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
FIRST_YEAR, LAST_YEAR = 2022, 2027  # anys amb dades a wip29
TZ = "Europe/Madrid"
ALL, ALL_CURRENT = "Tots", "Tots els actuals"  # opcions especials del selector d'espectacles

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

# Paleta validada per a daltonisme, en ordre fix: el període actual sempre és el blau.
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"]


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
    df = pd.DataFrame(events, columns=["id", "activity", "start", *RAW_COLUMNS[1:]]).drop_duplicates("id")
    df[RAW_COLUMNS[1:]] = df[RAW_COLUMNS[1:]].apply(pd.to_numeric, errors="coerce").fillna(0)
    # Variants of one name ("Los Hijos" / "Los Hijos.") become its most common spelling
    names = df["activity"].map(show_name)
    keys = names.map(squash)
    spelling = names.groupby(keys).agg(lambda s: s.value_counts().index[0])
    return df.assign(shows=1, activity=keys.map(spelling),
                     day=pd.to_datetime(df["start"]).dt.normalize()
                     ).drop(columns=["id", "start"])


# ---------------------------------------------------------------- periods

def shift_back(d: date, years: int, gran: str, is_end: bool = False) -> date:
    """Same date N years earlier. Days/weeks go back 52 weeks so weekdays line up."""
    if gran in ("D", "W"):
        return d - timedelta(weeks=52 * years)
    y = d.year - years
    last = monthrange(y, d.month)[1]
    if is_end and d.day == monthrange(d.year, d.month)[1]:
        return date(y, d.month, last)  # final de mes -> final de mes (febrers de traspàs)
    return date(y, d.month, min(d.day, last))


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


def plan(start: date, end: date, gran: str, years_back: int) -> pd.DataFrame:
    """One row per (year offset, bucket): its dates and how to label it."""
    base = split(start, end, gran)
    rows = []
    for k in range(years_back + 1):
        a, b = start.year - k, end.year - k
        shifted = split(shift_back(start, k, gran), shift_back(end, k, gran, is_end=True), gran)
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


def chart(table: pd.DataFrame, metric: str, colors: dict[str, str]):
    unit = METRICS[metric][0]
    n_labels = table["label"].nunique()
    kwargs = dict(x="series" if n_labels == 1 else "label", y=metric, color="series",
                  color_discrete_map=colors, custom_data=["dates"],
                  category_orders={"series": list(colors)[::-1], "label": list(dict.fromkeys(table["label"]))})
    if n_labels > 16:  # massa barres per llegir-les: línies
        fig = px.line(table, **kwargs, markers=n_labels <= 60)
        fig.update_traces(line_width=2, marker_size=8)
    else:
        fig = px.bar(table, **kwargs, barmode="group")
    fig.update_traces(hovertemplate=f"%{{fullData.name}} · %{{customdata[0]}}: "
                                    f"<b>%{{y:,.{2 if unit else 0}f}} {unit}</b><extra></extra>")
    fig.update_layout(
        title=metric, separators=",.", height=380, hovermode="x unified",
        xaxis_title=None, yaxis_title=None, yaxis_ticksuffix=f" {unit}" if unit else "",
        legend=dict(title=None, orientation="h", y=1.02, yanchor="bottom", x=1, xanchor="right"),
        bargap=0.25, bargroupgap=0.06, barcornerradius=4, margin=dict(l=8, r=8, t=56, b=8),
        xaxis_automargin=True, yaxis_automargin=True,
    )
    return fig


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

    today = barcelona_today()
    limits = dict(min_value=date(FIRST_YEAR, 1, 1), max_value=date(LAST_YEAR, 12, 31), format="DD/MM/YYYY")
    c1, c2, c3, c4 = st.columns(4)
    start = c1.date_input("Des de", date(today.year, 1, 1), **limits)
    end_box = c2.container()  # the toggle decides the date input, but sits below it
    until_today = c2.toggle("Fins avui")
    end = end_box.date_input("Fins a", today, **limits, disabled=until_today, key=f"end_{until_today}")
    if until_today:
        end = today  # always the current date, even if the app stays open past midnight
    gran = GRANULARITIES[c3.selectbox("Agrupa per", list(GRANULARITIES), index=2)]
    max_back = min(len(PALETTE_LIGHT) - 1, start.year - FIRST_YEAR)  # no data before FIRST_YEAR
    # At the maximum until the user moves it; then their choice, capped to what fits
    chosen = st.session_state.get("years_back") if st.session_state.get("years_moved") else max_back
    st.session_state["years_back"] = min(chosen, max_back)
    years_back = st.slider("Anys anteriors a comparar", 0, max_back, key="years_back",
                           on_change=lambda: st.session_state.update(years_moved=True)) if max_back else 0
    if start > end:
        st.error("La data d'inici és posterior a la data final.")
        return
    # Actuals: amb alguna funció a la temporada de wip29 en curs (1 de setembre - 31 d'agost)
    season_start = pd.Timestamp(today.year - (today.month < 9), 9, 1)
    current = set(events.loc[events["day"].between(season_start, season_start + pd.DateOffset(years=1, days=-1)),
                             "activity"])
    past = set(events["activity"]) - current
    options = [ALL, ALL_CURRENT, *sorted(current, key=squash), *sorted(past, key=squash)]
    picked = c4.multiselect("Espectacle", options, key="shows", on_change=reset_if_all, select_all=False,
                            format_func=lambda s: f"{s} (antic)" if s in past else s, placeholder=ALL)
    exclude = c4.toggle("Exclou els triats", disabled=not picked)
    shows = set(picked) - {ALL_CURRENT} | (current if ALL_CURRENT in picked else set())
    if picked:
        events = events[events["activity"].isin(shows) != exclude]
    title = "Tots els espectacles" if not picked else ("Tots excepte " if exclude else "") + ", ".join(picked)
    buckets = plan(start, end, gran, years_back)
    show_results(buckets, bucketize(events, buckets), title)


def show_results(buckets: pd.DataFrame, sums: pd.DataFrame, title: str) -> None:
    st.divider()
    st.subheader(title)
    if not sums["shows"].any():
        st.warning("No hi ha dades d'aquests espectacles en aquest període.")
        return
    metrics = st.multiselect("Mètriques", list(METRICS), default=["Recaptació", "Espectadors totals"])
    if not metrics:
        st.info("Tria almenys una mètrica.")
        return

    series = list(dict.fromkeys(buckets["series"]))  # ordenat per offset: actual primer
    dark = getattr(getattr(st.context, "theme", None), "type", None) == "dark"
    colors = dict(zip(series, PALETTE_DARK if dark else PALETTE_LIGHT))

    table = buckets.join(metric_table(sums, metrics).reset_index(drop=True))

    # Xifres clau: el període triat en gran i, a sota, cada any anterior amb la variació respecte a ell
    totals = metric_table(sums.groupby(level="offset").sum(), metrics)
    if len(series) > 1:
        st.caption(f"Els percentatges són la variació de {series[0]} respecte a cada any.")
    cols = []
    for i, m in enumerate(metrics):
        unit, now = METRICS[m][0], totals.loc[0, m]
        lines = []
        for k in range(1, len(series)):
            then = totals.loc[k, m]
            line = f"**{series[k]}** · {fmt(then, unit)}"
            if then and pd.notna(then) and pd.notna(now):
                pct = (now - then) / abs(then) * 100
                arrow, color = ("▲", "green") if pct >= 0 else ("▼", "red")
                line += f" · :{color}[{arrow} {pct:+.1f} %]".replace(".", ",")
            lines.append(line)
        if i % 2 == 0:
            cols = st.columns(2)  # two cards per row; each row its own columns so rows line up
        with cols[i % 2].container(border=True):
            st.metric(f"{m} · {series[0]}", fmt(now, unit))
            if lines:
                st.caption("  \n".join(lines))

    for m in metrics:
        st.plotly_chart(chart(table, m, colors), width="stretch", config={"displaylogo": False})

    with st.expander("Taula de dades"):
        chronological = table.sort_values(["offset", "idx"], ascending=[False, True])
        st.dataframe(chronological.drop(columns=["offset", "idx", "start", "end"]).rename(
            columns={"series": "Any", "label": "Període", "dates": "Dates"}),
            width="stretch", hide_index=True)


if __name__ == "__main__":
    st.set_page_config(page_title="Comparativa de taquilla · Teatreneu", page_icon="🎭")
    if "http" in st.session_state:
        dashboard()
    else:
        login_page()
