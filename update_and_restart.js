// Back-compat alias. "Update & Restart" and "Update" are now the same thing:
// update.js is mode-aware and always restarts the real server. The menu points
// only at update.js now; this file just forwards there so any cached menu entry
// or deep link still runs the correct unified flow.
module.exports = {
  run: [
    {
      method: "script.start",
      params: { uri: "update.js" }
    }
  ]
}
