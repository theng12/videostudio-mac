// Remove the Video Studio KH always-on startup service.
// Thin wrapper around uninstall_service.sh. macOS only.
module.exports = {
  run: [
    {
      method: "shell.run",
      params: {
        message: [
          "bash uninstall_service.sh"
        ]
      }
    }
  ]
}
