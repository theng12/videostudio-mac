// Force-restart the Video Studio KH startup service (launchd kickstart -k).
module.exports = {
  run: [
    { method: "shell.run", params: { message: [ "bash restart_service.sh" ] } }
  ]
}
