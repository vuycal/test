"""Dash page for CurtailOverload_v5.py, built the same way as tab.py.

Register layout(session) the same way the power-flow tab is registered.
Run Curtailment writes the choices on this page to a settings file and
starts CurtailOverload_v5.py, which is in this same folder.
"""

from app import app
from app import *
import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Input, Output, State
from dash import callback_context as ctx
from threading import Thread
from session import SESSIONS as sessions
import base64
import json
import os
import subprocess
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
CURTAIL_V5 = os.environ.get(
    "CURTAIL_V5",
    os.path.join(script_dir, "CurtailOverload_v5.py"),
)

Albregions = [
    "Calgary", "CentralEast", "CentralWest", "Central", "Edmonton",
    "NorthWest", "NorthEast", "SouthEast", "SouthWest", "BC",
]
Areas_dict = {
    "Calgary": [57, 6],
    "CentralEast": [42, 36, 37, 32, 13, 56, 28],
    "CentralWest": [30, 35, 34, 38, 39, 29],
    "Central": [30, 35, 34, 38, 39, 29, 42, 36, 37, 32, 13, 56, 28],
    "Edmonton": [40, 60, 31],
    "NorthWest": [17, 18, 19, 20, 21, 23, 26, 24, 22],
    "NorthEast": [25, 27, 33],
    "SouthEast": [43, 48, 47, 52, 54, 4],
    "SouthWest": [44, 45, 46, 49, 53, 55],
    "BC": [15],
}
Areas_lst = [
    4, 6, 13, 15, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
    31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 42, 43, 44, 45, 46, 47, 48, 49,
    52, 53, 54, 55, 56, 57, 60,
]
TECHNOLOGIES = [
    "CC", "COGEN", "GAS_CtG", "HYDRO", "IMPORTS", "OTHER", "SC", "SOLAR", "STORAGE", "WIND",
]


class CurtailThread(Thread):
    """Start CurtailOverload_v5.py with the settings chosen on this page."""

    def __init__(self, session_id, settings):
        Thread.__init__(self)
        self.session_id = session_id
        self.settings = settings

    def run(self):
        session = sessions.get_session(self.session_id)
        status = "Failed"
        try:
            folder = session.get_data("input_dir")
            settings_path = os.path.join(folder, "curtail_settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump(self.settings, handle)
            environment = dict(os.environ, CURTAIL_SETTINGS=settings_path)
            results = subprocess.run(
                [sys.executable, CURTAIL_V5],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=environment,
            )
            output = results.stdout or ""
            print(output)
            if '"success": true' in output.lower() or '"success":true' in output.lower():
                status = "Success"
            last_line = ""
            for line in output.splitlines():
                if "PSSE_RESULT:" in line or "Workbook written" in line:
                    last_line = line.strip()
            session.update_data("curtail_last_output", last_line)
        except Exception as exc:
            session.update_data("curtail_last_output", str(exc))
        if status == "Success":
            session.update_data("CurtailOverloadOccupied", "Done")
        else:
            session.update_data("CurtailOverloadOccupied", "Failed")


def _box(label, control):
    return html.Div([
        html.Label(label, style={"fontSize": "13px", "display": "block"}),
        control,
    ], style={"display": "inline-block", "margin": "0 12px 8px 0", "verticalAlign": "top"})


def layout(session):
    styles = {
        "container": {
            "maxWidth": "900px", "margin": "0 auto", "padding": "20px",
            "border": "1px solid #d3d3d3", "borderRadius": "5px",
            "background": "#f9f9f9", "boxShadow": "0 2px 5px rgba(0, 0, 0, 0.1)",
        },
        "title": {"textAlign": "center", "marginBottom": "10px", "color": "#333", "fontSize": "16px"},
        "input-field": {
            "marginBottom": "10px", "width": "100%", "padding": "5px",
            "border": "1px solid #ccc", "borderRadius": "3px",
        },
        "start-button_left": {
            "backgroundColor": "#4CAF50", "border": "none", "color": "white",
            "padding": "8px 16px", "display": "inline-block", "fontSize": "14px",
            "marginBottom": "10px", "cursor": "pointer", "borderRadius": "3px",
            "marginRight": "10px",
        },
        "custom-text": {"fontSize": "14px", "fontWeight": "normal"},
        "dropdown": {"marginBottom": "10px"},
    }
    number = {"width": "80px"}
    return html.Div(style=styles["container"], children=[
        html.H1("Curtail Overload", style=styles["title"]),
        dcc.Interval(id="co_interval", n_intervals=0, interval=1000 * 30),
        html.H3("Cluster Region", style=styles["custom-text"]),
        dcc.Dropdown(id="co_Regions", options=Albregions, multi=False, style=styles["dropdown"]),
        html.H3("Monitored study areas", style=styles["custom-text"]),
        dcc.Dropdown(id="co_StudyAreas", options=Areas_lst, multi=True, style=styles["dropdown"]),
        html.H3("Dispatch-down areas", style=styles["custom-text"]),
        dcc.Dropdown(id="co_DispatchDown", options=Areas_lst, multi=True, style=styles["dropdown"]),
        html.H3("Dispatch-up areas", style=styles["custom-text"]),
        dcc.Dropdown(id="co_DispatchUp", options=Areas_lst, multi=True, style=styles["dropdown"]),
        html.Div([
            dcc.Input(id="co_case_dir", type="text", placeholder="Folder that contains the .sav file...", style=styles["input-field"]),
            html.Button(id="co_scan", children="Scan", n_clicks=0, style=styles["start-button_left"]),
        ]),
        html.Label("Saved case", style=styles["custom-text"]),
        dcc.Dropdown(id="co_sav", options=[{"label": "", "value": ""}], value="", multi=False, style=styles["dropdown"]),
        html.Div([
            _box("Study", dcc.Dropdown(id="co_study", options=["N-0", "N-1", "N-1-1", "ALL"], value="ALL", clearable=False, style={"width": "140px"})),
            _box("Season", dcc.Dropdown(id="co_season", options=["SP", "WP"], value="SP", clearable=False, style={"width": "100px"})),
            _box("N-1-1 solution", dcc.Dropdown(id="co_solution", options=["AC", "DC"], value="DC", clearable=False, style={"width": "100px"})),
            _box("N-1-1 mode", dcc.Dropdown(id="co_n11_mode", options=["COMMON", "PER_OUTAGE"], value="COMMON", clearable=False, style={"width": "150px"})),
            _box("N-1 mode", dcc.Dropdown(id="co_n1_mode", options=["COMMON", "ACCC"], value="COMMON", clearable=False, style={"width": "130px"})),
        ]),
        html.Div([
            _box("Monitor min kV", dcc.Input(id="co_min_kv", type="number", value=69, style=number)),
            _box("Monitor max kV", dcc.Input(id="co_max_kv", type="number", value=999, style=number)),
            _box("Single-line min kV", dcc.Input(id="co_line_min_kv", type="number", value=69, style=number)),
            _box("Single-line max kV", dcc.Input(id="co_line_max_kv", type="number", value=999, style=number)),
            _box("Second-level min kV", dcc.Input(id="co_second_min_kv", type="number", value=69, style=number)),
            _box("Second-level max kV", dcc.Input(id="co_second_max_kv", type="number", value=999, style=number)),
            _box("Thermal limit %", dcc.Input(id="co_limit", type="number", value=100, style=number)),
            _box("Target %", dcc.Input(id="co_target", type="number", value=90, style=number)),
            _box("Watch %", dcc.Input(id="co_watch", type="number", value=100, style=number)),
        ]),
        dcc.Checklist(
            id="co_sources",
            options=[
                {"label": " Use the uploaded contingency file", "value": "special"},
                {"label": " Generate single-line outages", "value": "single"},
                {"label": " Monitor ties", "value": "ties"},
                {"label": " Make the N-0 free case", "value": "n0free"},
                {"label": " Save one mitigated case per N-1 contingency", "value": "mitigate_n1"},
            ],
            value=["special", "single", "ties", "n0free"],
            labelStyle={"display": "inline-block", "marginRight": "14px"},
        ),
        html.Div([
            html.Label("Dispatch down, technologies", style=styles["custom-text"]),
            dcc.Dropdown(id="co_tech_down", options=TECHNOLOGIES, value=[], multi=True, placeholder="Empty = every mapped type"),
            html.Label("Dispatch up, technologies", style=styles["custom-text"]),
            dcc.Dropdown(id="co_tech_up", options=TECHNOLOGIES, value=["CC", "COGEN", "GAS_CtG", "HYDRO", "OTHER", "SC"], multi=True),
        ]),
        dcc.Checklist(
            id="co_unmapped",
            options=[
                {"label": " Unmapped generators may dispatch down", "value": "down"},
                {"label": " Unmapped generators may dispatch up", "value": "up"},
            ],
            value=["down"],
            labelStyle={"display": "inline-block", "marginRight": "14px"},
        ),
        html.Div([
            html.Label("Swing buses (empty = every type-3 bus)", style=styles["custom-text"]),
            dcc.Input(id="co_swing", type="text", value="", placeholder="1520, 13000", style=styles["input-field"]),
            html.Label("Generators that must not move, bus numbers", style=styles["custom-text"]),
            dcc.Input(id="co_exclude", type="text", value="", style=styles["input-field"]),
            html.Label("PSS/E folder", style=styles["custom-text"]),
            dcc.Input(id="co_pssbin", type="text", value=r"C:\Program Files\PTI\PSSE35\35.4\PSSBIN", style=styles["input-field"]),
            _box("PSS/E bus dimension", dcc.Input(id="co_psse_buses", type="number", value=50000, style={"width": "120px"})),
            html.Label("Generator catalog workbook", style=styles["custom-text"]),
            dcc.Input(id="co_catalog", type="text", value=os.path.join(script_dir, "Merged_MPID_Facility_List.xlsx"), style=styles["input-field"]),
            html.Label("Catalog sheet", style=styles["custom-text"]),
            dcc.Input(id="co_catalog_sheet", type="text", value="Merged MPID Data", style={"width": "240px"}),
        ]),
        html.H3("Advanced", style=styles["custom-text"]),
        html.Div([
            _box("Max rounds", dcc.Input(id="co_rounds", type="number", value=20, style=number)),
            _box("Worst per round", dcc.Input(id="co_worst", type="number", value=3, style=number)),
            _box("Marginal %", dcc.Input(id="co_marginal", type="number", value=2, style=number)),
            _box("Step fractions", dcc.Input(id="co_fractions", type="text", value="1, 0.5, 0.25", style={"width": "140px"})),
            _box("Targets", dcc.Input(id="co_targets_n", type="number", value=8, style=number)),
            _box("Protected", dcc.Input(id="co_protect_n", type="number", value=10, style=number)),
            _box("Protect at %", dcc.Input(id="co_common_watch", type="number", value=100, style=number)),
            _box("Plan down to %", dcc.Input(id="co_common_target", type="number", value=97, style=number)),
        ]),
        dcc.Checklist(id="co_coupling", options=[{"label": " One plan for all contingencies", "value": "yes"}], value=["yes"]),
        html.Div([
            _box("Second-level method", dcc.Dropdown(id="co_second_method", options=["ACCC", "AUTO", "EXPLICIT"], value="ACCC", clearable=False, style={"width": "140px"})),
            _box("AC screen margin %", dcc.Input(id="co_ac_margin", type="number", value=5, style=number)),
            _box("DC screen margin %", dcc.Input(id="co_dc_margin", type="number", value=15, style=number)),
            _box("Explicit up to", dcc.Input(id="co_explicit_max", type="number", value=30, style=number)),
            _box("Workers", dcc.Input(id="co_workers", type="number", value=12, style=number)),
            _box("Parallel screen from", dcc.Input(id="co_par_screen", type="number", value=200, style=number)),
            _box("Parallel pairs from", dcc.Input(id="co_par_pairs", type="number", value=400, style=number)),
            _box("N-1 cap (0 = all)", dcc.Input(id="co_n1_cap", type="number", value=0, style=number)),
            _box("N-1-1 cap (0 = all)", dcc.Input(id="co_n11_cap", type="number", value=0, style=number)),
        ]),
        html.Div([
            _box("Top generators (0 = all)", dcc.Input(id="co_top_n", type="number", value=0, style=number)),
            _box("Min effectiveness", dcc.Input(id="co_min_eff", type="number", value=0.0001, style={"width": "100px"})),
            _box("Min relief MW", dcc.Input(id="co_min_relief", type="number", value=0.01, style=number)),
            _box("Max neutral", dcc.Input(id="co_max_neutral", type="number", value=200, style=number)),
            _box("Report min", dcc.Input(id="co_sf_min", type="number", value=0.0001, style={"width": "100px"})),
            _box("Q mode", dcc.Dropdown(id="co_q_mode", options=["PF", "HOLD"], value="PF", clearable=False, style={"width": "100px"})),
            _box("Iterations", dcc.Input(id="co_iters", type="number", value=20, style=number)),
            _box("Swing tolerance MW", dcc.Input(id="co_swing_tol", type="number", value=5, style=number)),
        ]),
        html.Label("Study power-flow options", style=styles["custom-text"]),
        dcc.Input(id="co_fnsl_study", type="text", value="0, 0, 0, 1, 2, 0, 99, 0", style=styles["input-field"]),
        html.Label("Curtailment power-flow options", style=styles["custom-text"]),
        dcc.Input(id="co_fnsl_curtail", type="text", value="0, 0, 0, 0, 2, 0, 0, 0", style=styles["input-field"]),
        html.H3("Contingency file", style=styles["custom-text"]),
        html.Div(id="co_con_status", style=styles["custom-text"]),
        dcc.Upload(
            id="co_upload_con",
            children=html.Div(["Drag and drop or click to select a .con file."]),
            style={
                "width": "100%", "height": "60px", "lineHeight": "60px",
                "borderWidth": "1px", "borderStyle": "dashed", "borderRadius": "5px",
                "textAlign": "center", "margin": "10px 0",
            },
            multiple=False,
        ),
        html.Button("Run Curtailment", id="co_start", n_clicks=0, style=styles["start-button_left"]),
        html.Div(id="co_status", style=styles["custom-text"]),
    ])


def _checked(value, token):
    return token in (value or [])


def _numbers(text, kind=float):
    if text is None or str(text).strip() == "":
        return []
    return [kind(part.strip()) for part in str(text).replace(";", ",").split(",") if part.strip()]


def _session_folder(session, session_id):
    try:
        folder = session.get_data("input_dir")
    except Exception:
        folder = None
    if not folder:
        folder = os.path.join(os.getcwd(), str(session_id))
        session.update_data("input_dir", folder)
    if not os.path.exists(folder):
        os.makedirs(folder)
    return folder


@app.callback(
    Output("co_StudyAreas", "value"),
    Output("co_DispatchDown", "value"),
    Output("co_DispatchUp", "value"),
    Input("co_Regions", "value"),
)
def co_region_areas(region):
    if not region:
        return [], [], []
    areas = Areas_dict.get(region, [])
    return areas, areas, areas


@app.callback(
    Output("co_scan", "children"),
    Output("co_sav", "options"),
    Input("co_scan", "n_clicks"),
    State("co_case_dir", "value"),
)
def co_scan_cases(n_clicks, folder):
    if not n_clicks or not folder or not os.path.isdir(folder):
        return "Scan", [{"label": "", "value": ""}]
    names = sorted(name for name in os.listdir(folder) if name.lower().endswith(".sav"))
    options = [{"label": name, "value": name} for name in names]
    return "Scan", options or [{"label": "(no .sav files)", "value": ""}]


@app.callback(
    Output("co_status", "children"),
    Output("co_start", "children"),
    Output("co_start", "disabled"),
    Output("co_con_status", "children"),
    Input("co_start", "n_clicks"),
    Input("co_interval", "n_intervals"),
    Input("co_upload_con", "filename"),
    Input("co_upload_con", "contents"),
    State("co_case_dir", "value"),
    State("co_sav", "value"),
    State("co_StudyAreas", "value"),
    State("co_DispatchDown", "value"),
    State("co_DispatchUp", "value"),
    State("co_study", "value"),
    State("co_season", "value"),
    State("co_solution", "value"),
    State("co_n11_mode", "value"),
    State("co_n1_mode", "value"),
    State("co_min_kv", "value"),
    State("co_max_kv", "value"),
    State("co_line_min_kv", "value"),
    State("co_line_max_kv", "value"),
    State("co_second_min_kv", "value"),
    State("co_second_max_kv", "value"),
    State("co_limit", "value"),
    State("co_target", "value"),
    State("co_watch", "value"),
    State("co_sources", "value"),
    State("co_tech_down", "value"),
    State("co_tech_up", "value"),
    State("co_unmapped", "value"),
    State("co_swing", "value"),
    State("co_exclude", "value"),
    State("co_pssbin", "value"),
    State("co_psse_buses", "value"),
    State("co_catalog", "value"),
    State("co_catalog_sheet", "value"),
    State("co_rounds", "value"),
    State("co_worst", "value"),
    State("co_marginal", "value"),
    State("co_fractions", "value"),
    State("co_coupling", "value"),
    State("co_targets_n", "value"),
    State("co_protect_n", "value"),
    State("co_common_watch", "value"),
    State("co_common_target", "value"),
    State("co_second_method", "value"),
    State("co_ac_margin", "value"),
    State("co_dc_margin", "value"),
    State("co_explicit_max", "value"),
    State("co_workers", "value"),
    State("co_par_screen", "value"),
    State("co_par_pairs", "value"),
    State("co_n1_cap", "value"),
    State("co_n11_cap", "value"),
    State("co_top_n", "value"),
    State("co_min_eff", "value"),
    State("co_min_relief", "value"),
    State("co_max_neutral", "value"),
    State("co_sf_min", "value"),
    State("co_q_mode", "value"),
    State("co_iters", "value"),
    State("co_swing_tol", "value"),
    State("co_fnsl_study", "value"),
    State("co_fnsl_curtail", "value"),
    State("session", "data"),
)
def co_run(n_clicks, _intervals, con_name, con_content, case_dir, sav_name,
           study_areas, down_areas, up_areas, study, season, solution, n11_mode, n1_mode,
           min_kv, max_kv, line_min, line_max, second_min, second_max, limit, target, watch,
           sources, tech_down, tech_up, unmapped, swing, exclude, pssbin, psse_buses,
           catalog, catalog_sheet, rounds, worst, marginal, fractions, coupling, targets_n,
           protect_n, common_watch, common_target, second_method, ac_margin, dc_margin,
           explicit_max, workers, par_screen, par_pairs, n1_cap, n11_cap, top_n, min_eff,
           min_relief, max_neutral, sf_min, q_mode, iters, swing_tol, fnsl_study,
           fnsl_curtail, session_id):
    session = sessions.get_session(session_id)
    item = ctx.triggered[0]["prop_id"].split(".")[0] if ctx.triggered else ""
    message = ""
    button = "Run Curtailment"
    disabled = False
    con_message = ""
    try:
        occupied = session.get_data("CurtailOverloadOccupied")
    except Exception:
        occupied = None
    if item == "co_interval" and occupied == "Done":
        session.update_data("CurtailOverloadOccupied", None)
        try:
            detail = session.get_data("curtail_last_output") or ""
        except Exception:
            detail = ""
        return "Curtailment finished. " + detail, "Done", False, con_message
    if item == "co_interval" and occupied == "Failed":
        session.update_data("CurtailOverloadOccupied", None)
        try:
            detail = session.get_data("curtail_last_output") or ""
        except Exception:
            detail = ""
        return "Curtailment failed. " + detail, "Done", False, con_message
    if item == "co_interval" and occupied == "Processing":
        return "Curtailment is running.", "Processing", True, con_message

    if item == "co_upload_con" and con_name and con_content:
        folder = _session_folder(session, session_id)
        data = con_content.encode("utf8").split(b";base64,")[1]
        with open(os.path.join(folder, con_name), "wb") as handle:
            handle.write(base64.decodebytes(data))
        session.update_data("curtail_con_name", con_name)
        return message, button, disabled, "%s uploaded." % con_name

    if item == "co_start" and n_clicks:
        if occupied is not None:
            return "A curtailment run is already going.", "Processing", True, con_message
        if not case_dir or not sav_name:
            return "Scan the folder and select a saved case first.", button, False, con_message
        if not os.path.isfile(CURTAIL_V5):
            return "CurtailOverload_v5.py was not found at %s." % CURTAIL_V5, button, False, con_message
        folder = _session_folder(session, session_id)
        case_path = sav_name if os.path.isabs(str(sav_name)) else os.path.join(case_dir, sav_name)
        sources = sources or []
        stored_name = None
        try:
            stored_name = session.get_data("curtail_con_name")
        except Exception:
            stored_name = None
        con_path = os.path.join(folder, stored_name) if stored_name else ""
        if "special" in sources and (not con_path or not os.path.isfile(con_path)):
            return "Upload a contingency file, or turn that option off.", button, False, con_message
        work = os.path.join(os.path.dirname(case_path), "curtail_out")
        os.makedirs(work, exist_ok=True)
        settings = {
            "PSSBIN_PATH": pssbin or r"C:\Program Files\PTI\PSSE35\35.4\PSSBIN",
            "PSSE_BUSES": int(psse_buses or 50000),
            "CASE_FILE": case_path,
            "CON_FILE": con_path or os.path.join(folder, "none.con"),
            "USE_SPECIAL_CON": "special" in sources,
            "USE_SINGLE_LINE_CON": "single" in sources,
            "SINGLE_LINE_MIN_KV": float(line_min or 69),
            "SINGLE_LINE_MAX_KV": float(line_max or 999),
            "STUDY": study or "ALL",
            "N11_MODE": n11_mode or "COMMON",
            "COMMON_MAX_ROUNDS": int(rounds or 20),
            "COMMON_WORST_PER_ROUND": int(worst or 3),
            "MARGINAL_PCT": float(marginal or 0),
            "COMMON_STEP_FRACTIONS": _numbers(fractions) or [1.0, 0.5, 0.25],
            "COMMON_COUPLING": _checked(coupling, "yes"),
            "COMMON_COUPLED_TARGETS": int(targets_n or 8),
            "COMMON_COUPLED_PROTECT": int(protect_n or 10),
            "COMMON_WATCH_PCT": float(common_watch or 100),
            "COMMON_TARGET_PCT": float(common_target or 97),
            "SECOND_LEVEL_SCREEN_MARGIN_PCT": float(ac_margin or 5),
            "N11_SOLUTION": solution or "DC",
            "DC_SCREEN_MARGIN_PCT": float(dc_margin or 15),
            "SECOND_LEVEL_METHOD": second_method or "ACCC",
            "SECOND_LEVEL_EXPLICIT_MAX_PAIRS": int(explicit_max or 30),
            "SECOND_LEVEL_MIN_KV": float(second_min or line_min or 69),
            "SECOND_LEVEL_MAX_KV": float(second_max or 999),
            "MAX_WORKERS": int(workers or 1),
            "PARALLEL_MIN_SCREEN": int(par_screen or 200),
            "PARALLEL_MIN_PAIRS": int(par_pairs or 400),
            "MAKE_N0_FREE_CASE": "n0free" in sources,
            "MITIGATE_N1": "mitigate_n1" in sources,
            "N1_MODE": n1_mode or "COMMON",
            "MAX_N1_CONTINGENCIES": int(n1_cap or 0),
            "MAX_N11_FIRST_OUTAGES": int(n11_cap or 0),
            "STUDY_AREAS": [int(area) for area in (study_areas or [])],
            "MONITOR_TIES": "ties" in sources,
            "MIN_KV": float(min_kv or 69),
            "MAX_KV": float(max_kv or 999),
            "GENERATOR_CATALOG_FILE": catalog or os.path.join(script_dir, "Merged_MPID_Facility_List.xlsx"),
            "GENERATOR_CATALOG_SHEET": catalog_sheet or "Merged MPID Data",
            "ALLOW_UNMAPPED_DISPATCH_DOWN": "down" in (unmapped or []),
            "ALLOW_UNMAPPED_DISPATCH_UP": "up" in (unmapped or []),
            "DISPATCH_DOWN_TECHNOLOGIES": list(tech_down or []),
            "DISPATCH_UP_TECHNOLOGIES": list(tech_up or []),
            "DISPATCH_DOWN_AREAS": [int(area) for area in (down_areas or [])],
            "DISPATCH_UP_AREAS": [int(area) for area in (up_areas or [])],
            "SWING_BUSES": _numbers(swing, int),
            "EXCLUDE_GEN_BUSES": _numbers(exclude, int),
            "THERMAL_LIMIT_PCT": float(limit or 100),
            "TARGET_LOADING_PCT": float(target or 90),
            "WATCH_PCT": float(watch or 100),
            "TOP_N_GENS": int(top_n or 0),
            "MIN_EFFECTIVENESS": float(min_eff or 0.0001),
            "MIN_RELIEF_MW": float(min_relief or 0.01),
            "MAX_NEUTRAL_GENS": int(max_neutral or 200),
            "SF_REPORT_MIN": float(sf_min or 0.0001),
            "Q_MODE": q_mode or "PF",
            "MAX_ITERS": int(iters or 20),
            "SWING_TOL_MW": float(swing_tol or 5),
            "FNSL_STUDY": _numbers(fnsl_study, int) or [0, 0, 0, 1, 2, 0, 99, 0],
            "FNSL_CURTAIL": _numbers(fnsl_curtail, int) or [0, 0, 0, 1, 2, 0, 0, 0],
            "WORK_DIR": work,
            "SAVE_CASE": os.path.join(work, "N0_free.sav"),
            "COMMON_SAVE_CASE": os.path.join(work, "N1_free_common.sav"),
        }
        session.update_data("CurtailOverloadOccupied", "Processing")
        session.update_data("curtail_last_output", "")
        CurtailThread(session_id, settings).start()
        return "Curtailment started. Output folder: %s" % work, "Processing", True, con_message
    return message, button, disabled, con_message
