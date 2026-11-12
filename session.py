"""Small session store used by the local dashboard."""


class Session:
    def __init__(self):
        self.data = {}

    def get_data(self, key):
        return self.data.get(key)

    def update_data(self, key, value):
        self.data[key] = value


class Sessions:
    def __init__(self):
        self._sessions = {}

    def add_session(self, session_id):
        if session_id not in self._sessions:
            self._sessions[session_id] = Session()

    def get_session(self, session_id):
        self.add_session(session_id)
        return self._sessions[session_id]


SESSIONS = Sessions()
