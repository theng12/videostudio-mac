// Install Video Studio KH as an always-on macOS startup service.
// Thin wrapper around install_service.sh (which holds all the launchd logic so
// it's easy to read / run by hand too). macOS only.
module.exports = {
  run: [
    {
      method: "shell.run",
      params: {
        message: [
          "bash install_service.sh"
        ]
      }
    }
  ]
}
