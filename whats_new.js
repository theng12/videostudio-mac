// Persistent release-notes action. Keeping this as a launcher script means it
// works whether the web server is stopped, managed by start.js, or running as
// the startup service.
module.exports = {
  run: [
    {
      method: "fs.cat",
      params: {
        path: "CHANGELOG.md"
      }
    }
  ]
}
