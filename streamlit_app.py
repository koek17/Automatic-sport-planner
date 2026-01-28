# 
import streamlit as st
import pandas as pd
import requests
from datetime import datetime, date, time, timedelta

st.markdown("""
<style>
/* Maak alle widgets even strak */
div[data-testid="stVerticalBlock"] > div { padding-top: 0.25rem; padding-bottom: 0.25rem; }
div[data-testid="stSelectbox"], div[data-testid="stTextInput"] { margin-top: 0.25rem; margin-bottom: 0.25rem; }
</style>
""", unsafe_allow_html=True)

DAYS_NL = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]
SHIFT_PRESETS = {
    "Geen werk ": ("", ""),
    "Ochtend (07:00–15:00)": ("07:00", "15:00"),
    "Middag (13:00–21:00)": ("13:00", "21:00"),
    "Middag (14:00–22:00)": ("14:00", "22:00"),
    "Avond (18:00–00:00)": ("18:00", "00:00"),
    "Custom…": ("", ""),
}

CITY_PRESETS = {
    "Amsterdam": (52.3676, 4.9041),
    "Den Haag": (52.0833, 4.3000),
    "Ede": (52.249375, 5.616126),
    "custom": (None, None),
}

# helpers


def parse_hhmm(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        return time(int(hh), int(mm))
    except Exception:
        return None


def minutes_between(t1: time, t2: time) -> int:
    dt1 = datetime.combine(date.today(), t1)
    dt2 = datetime.combine(date.today(), t2)
    if dt2 <= dt1:
        return 0
    return int((dt2 - dt1).total_seconds() // 60)


def clamp_interval(start: time, end: time, window_start: time, window_end: time):
    if not start or not end:
        return None
    a = datetime.combine(date.today(), start)
    b = datetime.combine(date.today(), end)
    if b <= a:
        return None
    ws = datetime.combine(date.today(), window_start)
    we = datetime.combine(date.today(), window_end)
    a2 = max(a, ws)
    b2 = min(b, we)
    if b2 <= a2:
        return None
    return (a2.time(), b2.time())


def free_minutes_for_day(blocks, day_window):
    """blocks: list of dicts {name, start, end}"""
    window_start, window_end = day_window
    total = minutes_between(window_start, window_end)

    intervals = []
    for blk in blocks:
        clamped = clamp_interval(
            blk.get("start"), blk.get("end"), window_start, window_end
        )
        if clamped:
            intervals.append(clamped)

    intervals.sort()
    merged = []
    for s, e in intervals:
        if not merged:
            merged.append([s, e])
        else:
            last_s, last_e = merged[-1]
            if datetime.combine(date.today(), s) <= datetime.combine(
                date.today(), last_e
            ):
                if datetime.combine(date.today(), e) > datetime.combine(
                    date.today(), last_e
                ):
                    merged[-1][1] = e
            else:
                merged.append([s, e])

    busy = sum(minutes_between(s, e) for s, e in merged)
    return max(total - busy, 0), merged


def get_daily_max_temps(lat: float, lon: float, start: date, days: int, tz="Europe/Amsterdam"):
    end = start + timedelta(days=days - 1)
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "timezone": tz,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    return {
        data["daily"]["time"][i]: float(data["daily"]["temperature_2m_max"][i])
        for i in range(len(data["daily"]["time"]))
    }


# ---------- Planning logic ----------
def session_priority(row):
    """
    Higher = schedule earlier / on better day.
    Simple heuristic:
    - Longer rides higher
    - Types: endurance long > combi strength > cadence
    """
    base = row["duration_min"]
    t = (row["type"] or "").lower()

    type_bonus = 0
    if "duurtraining" in t:
        type_bonus = 400
    elif "kracht" in t:
        type_bonus = 250
    elif "cadans" in t:
        type_bonus = 150

    return base + type_bonus


def pick_best_day_for_session(days_df, used_days, session, temp_threshold):
    """
    Choose day with:
    - temp ok (if session is bike)
    - enough free minutes
    - best score: warmer + more free time
    """
    dur = int(session["duration_min"])
    must_be_warm = session["mode"] == "bike"
    best_idx = None
    best_score = -1e9

    for idx, d in days_df.iterrows():
        if idx in used_days:
            continue
        if d["free_min"] < dur:
            continue
        if must_be_warm and (pd.isna(d["temp_max"]) or d["temp_max"] < temp_threshold):
            continue

        # score: temp + free time
        temp = d["temp_max"] if not pd.isna(d["temp_max"]) else -50
        score = (temp * 10) + (d["free_min"] / 10)

        # tiny preference: weekend gets a nudge for long endurance
        if session["mode"] == "bike" and session["duration_min"] >= 180 and d["weekday_idx"] in (5, 6):
            score += 15

        if score > best_score:
            best_score = score
            best_idx = idx

    return best_idx


def build_plan(days_df, fondo_df, temp_threshold, gym_min, chestback_min, gym_sessions=4, rest_choice="Auto (drukste dag)"):
    """
    Returns days_df with columns:
    - plan_items (list)
    - notes
    """
    out = days_df.copy()
    out["plan_items"] = [[] for _ in range(len(out))]
    out["notes"] = ""

    # Sort Fondo sessions by priority
    fondo = fondo_df.copy()
    fondo["priority"] = fondo.apply(session_priority, axis=1)
    fondo = fondo.sort_values("priority", ascending=False)

    used_for_bike = set()

    # Place bike sessions first
    for _, ses in fondo.iterrows():
        best_day = pick_best_day_for_session(out, used_for_bike, ses, temp_threshold)
        if best_day is not None:
            used_for_bike.add(best_day)
            out.at[best_day, "plan_items"] = out.at[best_day, "plan_items"] + [f"🚴 {ses['name']} ({ses['duration_min']} min)"]
            out.at[best_day, "free_min"] -= int(ses["duration_min"])

            # mark intensity flags for gym restrictions
            t = (ses["type"] or "").lower()
            if "kracht" in t:
                out.at[best_day, "notes"] += "Zware combi-rit (kracht) → liever geen zware gym. "
            elif "duurtraining" in t and int(ses["duration_min"]) >= 180:
                out.at[best_day, "notes"] += "Lange duur → liever geen legs dezelfde dag. "
        else:
            # couldn't place
            out.loc[:, "notes"] = out["notes"]  # no-op to keep type stable

    # Decide where chest/back can be added on bike days (if time left)
    for idx, d in out.iterrows():
        has_bike = any(item.startswith("🚴") for item in d["plan_items"])
        if has_bike and d["free_min"] >= chestback_min:
            out.at[idx, "plan_items"] = out.at[idx, "plan_items"] + [f"🏋️ Chest/Back (kort, {chestback_min} min)"]
            out.at[idx, "free_min"] -= int(chestback_min)
            out.at[idx, "notes"] += "Chest/Back toegevoegd omdat er tijd is. "

    # -----------------------------
    # Gym planning
    # -----------------------------

    # hoeveel fietsdagen zijn er echt geplaatst?
    placed_bike_count = out["plan_items"].apply(lambda lst: any(item.startswith("🚴") for item in lst)).sum()

    # Als er NIET gefietst wordt (te koud / geen geschikte dagen):
    # -> herhaal gymschema met 1 rustdag (6 gym + 1 rust)
    if placed_bike_count == 0:
        gym_sessions_effective = 6
        gym_split = [
            "Legs 1",
            "Push 1 (chest/shoulders/tris)",
            "Pull 1 (back/bis)",
            "Legs 2",
            "Push 2",
            "Pull 2",
        ]

        # choose rest day
        if rest_choice == "Auto (drukste dag)":
            rest_idx = out["free_min"].idxmin()
        else:
            day_to_idx = {
                "Maandag": 0,
                "Dinsdag": 1,
                "Woensdag": 2,
                "Donderdag": 3,
                "Vrijdag": 4,
                "Zaterdag": 5,
                "Zondag": 6,
            }
            wanted = day_to_idx.get(rest_choice, 0)
            rest_idx = out.index[out["weekday_idx"] == wanted][0]

        # mark the rest day and note
        out.at[rest_idx, "plan_items"] = out.at[rest_idx, "plan_items"] + ["🛌 Rustdag"]
        out.at[rest_idx, "notes"] += "Geen fietsen (temp < drempel) → gymschema herhaalt met 1 rustdag. "
    else:
        # normaal gedrag: gym_sessions (bijv 4) zoals jij instelt
        gym_sessions_effective = gym_sessions
        gym_split = ["Legs 1", "Push (chest/shoulders/tris)", "Legs 2", "Pull (back/bis)"]
        gym_split = gym_split[:gym_sessions_effective]
        rest_idx = None

    # candidate days: genoeg tijd, en NIET de rustdag
    candidates = out.copy()
    candidates["has_bike"] = candidates["plan_items"].apply(lambda lst: any(item.startswith("🚴") for item in lst))

    if rest_idx is not None:
        candidates = candidates.drop(index=rest_idx)

    # Prefer non-bike days, then more free time
    candidates = candidates.sort_values(["has_bike", "free_min"], ascending=[True, False])

    placed = 0
    for idx, d in candidates.iterrows():
        if placed >= gym_sessions_effective:
            break
        if out.at[idx, "free_min"] < gym_min:
            continue

        # skip gym op zware combi-rit (kracht) dag
        if "Zware combi-rit (kracht)" in str(out.at[idx, "notes"]):
            continue

        # lange duur: liever geen legs diezelfde dag (alleen relevant als er wél gefietst is)
        avoid_legs = "Lange duur" in str(out.at[idx, "notes"])

        label = gym_split[placed]
        if avoid_legs and "Legs" in label:
            # swap with non-legs if possible
            for j in range(placed, len(gym_split)):
                if "Legs" not in gym_split[j]:
                    label = gym_split[j]
                    gym_split[j], gym_split[placed] = gym_split[placed], gym_split[j]
                    break

        out.at[idx, "plan_items"] = out.at[idx, "plan_items"] + [f"{label} ({gym_min} min)"]
        out.at[idx, "free_min"] -= int(gym_min)
        placed += 1

    return out


# ---------- UI ----------
st.set_page_config(page_title="Werk/Studie + FONDO + Gym Planner", layout="wide")
st.title("Weekplanner: Automatisch schema")

with st.sidebar:
    st.header("Benodigde informatie")

    city = st.selectbox("Locatie", list(CITY_PRESETS.keys()), key="city")
    preset_lat, preset_lon = CITY_PRESETS[city]

    # Zorg dat session_state keys bestaan
    st.session_state.setdefault("lat", 52.3676)
    st.session_state.setdefault("lon", 4.9041)

    if city != "Custom…":
        st.session_state["lat"] = preset_lat
        st.session_state["lon"] = preset_lon

    lat = st.session_state["lat"]
    lon = st.session_state["lon"]

    week_start = st.date_input("Week start (maandag)", value=date.today() - timedelta(days=date.today().weekday()))
    
    REST_OPTIONS = ["Auto (drukste dag)", "Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]
    rest_choice = st.selectbox("Rustdag", REST_OPTIONS, index=0)
   
    temp_threshold = st.slider("Minimum temperatuur voor fietsen (°C)", 0.0, 25.0, 10.0, 0.5)
    
    st.header("Dag-venster (vrije tijd)")
    day_start = st.text_input("Dag start (HH:MM)", value="07:00")
    day_end = st.text_input("Dag eind (HH:MM)", value="23:00")
    day_window = (parse_hhmm(day_start) or time(7, 0), parse_hhmm(day_end) or time(23, 0))

    st.header("Trainingstijden")
    gym_min = st.number_input("Gym sessie duur (min)", min_value=30, max_value=180, value=80, step=5)
    chestback_min = st.number_input("Chest/Back extra op fietsdag (min)", min_value=20, max_value=120, value=80, step=5)
    gym_sessions = st.number_input("Aantal gym sessies per week", min_value=1, max_value=7, value=4, step=1)

    

st.subheader("1) Vul je werk/studie in (wisselende week)")

cols = st.columns(7)
rows = []

for idx in range(7):
    d = week_start + timedelta(days=idx)
    with cols[idx]:
        st.markdown(f"#### {DAYS_NL[idx]}")
        st.caption(d.isoformat())

        # ---- Werkdienst dropdown ----
        shift_label = st.selectbox(
            "Werkdienst",
            list(SHIFT_PRESETS.keys()),
            key=f"shift_{idx}",
        )

        preset_start, preset_end = SHIFT_PRESETS[shift_label]

        start_key = f"w_s_{idx}"
        end_key = f"w_e_{idx}"

        st.session_state.setdefault(start_key, "")
        st.session_state.setdefault(end_key, "")

        if shift_label != "Custom…":
            st.session_state[start_key] = preset_start or ""
            st.session_state[end_key] = preset_end or ""

        w_s = st.text_input("Werk start", key=start_key, placeholder="14:00")
        w_e = st.text_input("Werk eind", key=end_key, placeholder="22:00")

        # ---- spacing ----
        st.write("")
        st.write("")

        # ---- Studie ----
        s_s = st.text_input("Studie start", key=f"s_s_{idx}", placeholder="09:00")
        s_e = st.text_input("Studie eind", key=f"s_e_{idx}", placeholder="12:00")

        # ---- Vrije tijd berekenen ----
        blocks = []
        if w_s and w_e:
            blocks.append({"name": "work", "start": parse_hhmm(w_s), "end": parse_hhmm(w_e)})
        if s_s and s_e:
            blocks.append({"name": "study", "start": parse_hhmm(s_s), "end": parse_hhmm(s_e)})

        free_min, merged = free_minutes_for_day(blocks, day_window)

        rows.append({
            "weekday_idx": idx,
            "day": DAYS_NL[idx],
            "date": d,
            "work": f"{w_s}-{w_e}" if w_s and w_e else "",
            "study": f"{s_s}-{s_e}" if s_s and s_e else "",
            "free_min": free_min,
        })


days_df = pd.DataFrame(rows)

st.divider()
st.subheader("2) FONDO trainingen")

default_fondo = pd.DataFrame([
    {"name": "Combi-duur-krachttraining", "duration_min": 140, "mode": "bike", "type": "combi kracht"},
    {"name": "Duurtraining",              "duration_min": 240, "mode": "bike", "type": "duurtraining"},
    {"name": "Combi-duur-cadanstraining", "duration_min": 80,  "mode": "bike", "type": "combi cadans"},
])

fondo_df = st.data_editor(
    default_fondo,
    use_container_width=True,
    num_rows="dynamic",
    column_config={
        "name": st.column_config.TextColumn("Training"),
        "duration_min": st.column_config.NumberColumn("Duur (min)", min_value=10, max_value=600, step=5),
        "mode": st.column_config.SelectboxColumn("Mode", options=["bike"], help="MVP: alleen bike sessies"),
        "type": st.column_config.TextColumn("Type (vrij)"),
    },
)

st.divider()
st.subheader("3) Genereer je weekplan")

if st.button("Maak weekplan"):
    try:
        temps = get_daily_max_temps(lat, lon, week_start, 7, tz="Europe/Amsterdam")
        days_df2 = days_df.copy()
        days_df2["temp_max"] = days_df2["date"].astype(str).map(temps)

        planned = build_plan(
            days_df=days_df2,
            fondo_df=fondo_df,
            temp_threshold=temp_threshold,
            gym_min=int(gym_min),
            chestback_min=int(chestback_min),
            gym_sessions=int(gym_sessions),
            rest_choice=rest_choice,
        )

        # Pretty output
        out = planned.copy()
        out["temp_max"] = out["temp_max"].round(1)
        out["vrije_tijd"] = (out["free_min"] // 60).astype(int).astype(str) + "u " + (out["free_min"] % 60).astype(int).astype(str) + "m"
        out["plan"] = out["plan_items"].apply(lambda lst: "\n".join(lst) if lst else "—")

        out = out[["day", "date", "work", "study", "vrije_tijd", "temp_max", "plan", "notes"]]
        st.dataframe(out, use_container_width=True)

        st.caption("Tip: zet je locatie (lat/lon) goed voor jouw plaats. Als het weer niet kan laden: probeer later opnieuw.")
    except Exception as e:
        st.error(f"Kon het weer niet ophalen of plannen faalde: {e}")
        st.info("Check je internetverbinding en of latitude/longitude klopt.")
