// Check whether the Video Studio KH startup service is running (launchd state +
// live /api/health + recent log). Output shows in the Pinokio terminal.
module.exports = {
  run: [
    { method: "shell.run", params: { message: [ "bash status_service.sh" ] } }
  ]
}
