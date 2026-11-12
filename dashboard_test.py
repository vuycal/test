"""Local dashboard for this project. One tab: Curtail Overload."""

import random
import string

import dash_bootstrap_components as dbc
import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Input, Output, State

from app import app
import CurtailOverload_tab
from session import SESSIONS as sessions

app.title = "Curtail Overload"

app.layout = html.Div([
    dbc.Container(
        [
            dbc.NavbarSimple(
                brand="Curtail Overload",
                brand_href="#",
                color="primary",
                dark=True,
            ),
        ],
        fluid=True,
    ),
    dcc.Store(id="session", storage_type="session"),
    dbc.Row(html.P()),
    dbc.Row(
        dbc.Tabs(
            [
                dbc.Tab(label="Curtail Overload", tab_id="tab-curtail"),
            ],
            id="tab-menu",
            active_tab="tab-curtail",
        ),
    ),
    dbc.Row(html.P()),
    html.Div(id="tab-content"),
])


@app.callback(
    Output("tab-content", "children"),
    Output("session", "data"),
    Input("tab-menu", "active_tab"),
    State("session", "data"),
)
def display_tab(active_tab, session_id):
    if session_id is None:
        session_id = "".join(
            random.choice(string.ascii_uppercase + string.digits) for _ in range(16)
        )
    sessions.add_session(session_id)
    session_obj = sessions.get_session(session_id)
    if session_obj.get_data("CurtailOverloadOccupied") is None and "CurtailOverloadOccupied" not in session_obj.data:
        session_obj.update_data("CurtailOverloadOccupied", None)

    layout = ""
    if active_tab == "tab-curtail":
        layout = CurtailOverload_tab.layout(session_obj)
    return layout, session_id


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=8050, use_reloader=False)
