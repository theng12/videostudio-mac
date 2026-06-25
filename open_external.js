// Open the WebUI in an external browser instead of Pinokio's embedded webview.
//
// Why this exists:
// Pinokio's in-app webview occasionally caches a broken state — black screen,
// stale assets, blocked SSE stream, etc. — and the user is stranded because
// they don't necessarily know the running port. This script is the escape
// hatch: pinokio.js passes the URL via params and we hand it off to the
// system default browser via `web.open` with target="_blank".
//
// Called from pinokio.js like:
//   { href: "open_external.js", params: { url: "http://localhost:47872" } }
module.exports = {
  run: [{
    method: "web.open",
    params: {
      uri: "{{args.url}}",
      target: "_blank"
    }
  }]
}
