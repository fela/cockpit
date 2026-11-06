// The one-time code travels in a fragment so it never reaches HTTP logs or referrers.
const code = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
if (code) {
  fetch("/auth/local", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  }).then(async (response) => {
    if (!response.ok) throw new Error("Sign-in code used or expired. Run cockpit login again.");
    const answer = await response.json();
    localStorage.setItem("cockpit.local-secret", answer.page_secret);
    location.replace("/");
  }).catch((error) => { document.getElementById("status").textContent = error.message; });
}
