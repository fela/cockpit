"use strict";

// Cockpit's reply to a read or an action, `{response, data}` as readThroughRestart and sendAction give it: its data,
// or an error in Cockpit's words that carries the status. A 401 means the sign-in ran out, and the page goes to sign in.
window.replyData = ({ response, data }) => {
  if (response.status === 401) {
    location.href = "/auth/google";
    throw Object.assign(new Error("Signed out"), { status: 401 });
  }
  if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
  return data;
};
